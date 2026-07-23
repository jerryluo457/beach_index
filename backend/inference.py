"""
inference.py — Model loading and prediction

ARCHITECTURE NOTE
-----------------
This module knows about PyTorch and images. It does NOT know about HTTP
(main.py) or beaches (config lives in sources.py). You hand it an image
array, it hands back numbers.

Deliberately beach-agnostic: the "is this beach supported by the sargassum
model?" check belongs in main.py. Keeping that out of here means this file
stays a pure image-in/prediction-out unit, testable without any app context.

LOADING STRATEGY (Decision: Option C — a class that owns model + config)
-----------------------------------------------------------------------
Loading happens in __init__, called ONCE from main.py's lifespan hook —
never at import time. Two reasons this matters:
  1. `import inference` shouldn't trigger an 85MB disk read as a side effect.
  2. The FastAPI lifespan pattern requires loading to be something you CALL.
The class bundles the model with its threshold/calibration config, so those
constants can't drift away from the model they belong to.
"""

from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import torch
import albumentations as A
from albumentations.pytorch import ToTensorV2
import segmentation_models_pytorch as smp
from skimage.morphology import remove_small_objects

# Single source of truth for the coverage→label thresholds.
# DESIGN CHOICE: import from scoring.py rather than re-defining the bands here.
# Duplicating them would guarantee they eventually disagree after one edit.
# ALTERNATIVE: return only the raw number and let main.py call scoring.py for
# the label — arguably cleaner layering (inference wouldn't depend on scoring
# at all), at the cost of the caller always needing two calls.
from scoring import sargassum_score


# ─────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────

IMG_SIZE = 512          # MUST match training. The model literally cannot
                        # accept a different size without changing behavior.

DEFAULT_THRESHOLD = 0.8 # Swept on the validation set: IoU went 0.314 @ 0.5 →
                        # 0.333 @ 0.8. Higher = more selective, which counters
                        # the model's observed tendency to over-predict.
                        # TRADEOFF: pushing higher would start eroding thin
                        # real wrack lines, so 0.8 is roughly the ceiling.

MIN_BLOB_PIXELS = 30    # Post-processing: drop isolated specks smaller than
                        # this (at 512×512 resolution). Kills the scattered
                        # false-positive dots seen in validation WITHOUT
                        # raising the threshold further — which is the whole
                        # point, since a higher threshold would also delete
                        # legitimate thin sargassum.

# ⚠️ CALIBRATION — THIS VALUE IS NOT YET FITTED. ⚠️
# Set to 1.0 (no-op) deliberately. The model is known to over-predict area,
# so a <1.0 factor is justified IN PRINCIPLE — but the actual number must be
# DERIVED from your validation set, not guessed. See fit_calibration() at the
# bottom of this file for how to compute it.
#
# Until you run that: leaving this at 1.0 is the honest choice, and it's also
# self-consistent, because scoring.py's band thresholds (0.3/1.0/1.5%) were
# eyeballed from UNCALIBRATED model output. If you change this constant, you
# MUST re-check those bands or every beach silently shifts a category.
COVERAGE_CALIBRATION = 1.0


# ─────────────────────────────────────────────────────────────────────────
# PREPROCESSING
# ─────────────────────────────────────────────────────────────────────────
# This is NOT a design choice — it's a correctness constraint. The model was
# trained on 512×512 ImageNet-normalized RGB. Any deviation here degrades
# predictions SILENTLY: no error, just worse output. This must stay byte-for-
# byte equivalent to the `val_tf` used in training (note: val_tf, not
# train_tf — no augmentation at inference).
_tf = A.Compose([
    A.Resize(IMG_SIZE, IMG_SIZE),
    A.Normalize(),      # ImageNet mean/std, matching encoder_weights="imagenet"
    ToTensorV2(),       # HWC uint8 → CHW float tensor
])


# ─────────────────────────────────────────────────────────────────────────
# SARGASSUM SEGMENTATION
# ─────────────────────────────────────────────────────────────────────────

class SargassumDetector:
    """U-Net segmenter: beach photo in → sargassum mask + coverage % out.

    Trained on ~50 hand-labeled frames from the Lake Worth pier cam.
    Validated IoU ≈ 0.33 (threshold 0.8). Generalizes to Boynton (same camera
    vendor/angle); degrades on Jupiter/Boca — hence the supported/beta tiering,
    which is enforced by the CALLER, not here.
    """

    def __init__(self,
                 weights_path: str,
                 threshold: float = DEFAULT_THRESHOLD,
                 device: str = "cpu"):
        """
        device="cpu" default: a single 512×512 image through a ResNet34 U-Net
        is sub-second on CPU, so the deployed backend needs no GPU. GPU stays
        a training-only concern. This keeps hosting cheap (Railway/Render
        free tiers are CPU-only) and the Docker image far smaller.
        """
        self.threshold = threshold
        self.device = device

        # encoder_weights=None — NOT "imagenet". We're about to overwrite every
        # weight with our trained checkpoint, so downloading ImageNet weights
        # first would be a pointless ~85MB download on every container start.
        self.model = smp.Unet(
            encoder_name="resnet34",
            encoder_weights=None,
            in_channels=3,
            classes=1,          # binary: sargassum vs. not
        )

        # map_location forces weights onto the target device regardless of
        # where they were saved from (they were saved from a CUDA session in
        # Colab — without this, loading on a CPU-only host raises).
        state = torch.load(weights_path, map_location=device)
        self.model.load_state_dict(state)

        self.model.to(device)

        # .eval() is NOT optional. It switches BatchNorm to use running
        # statistics instead of batch statistics, and disables dropout.
        # Forgetting it produces plausible-looking but wrong output — one of
        # the classic silent PyTorch serving bugs.
        self.model.eval()

    def predict(self,
                image: np.ndarray,
                zone_mask: Optional[np.ndarray] = None,
                save_mask_to: Optional[str] = None) -> dict:
        """Run the model on one image.

        Args:
            image: RGB uint8 array (H, W, 3). CALLER converts from BGR —
                   see the note in the module docstring of main.py. cv2.imread
                   returns BGR; feeding that here channel-swaps the input and
                   quietly wrecks predictions.
            zone_mask: Optional binary mask of the sand region. Not used yet —
                   the parameter exists so switching from whole-frame coverage
                   to sand-zone-relative coverage later doesn't require
                   changing this function's signature or its callers.
            save_mask_to: Optional path to write the mask PNG for UI overlay.

        Returns:
            dict with coverage_pct, score, label, and mask metadata.
        """
        # ── preprocess ──
        tensor = _tf(image=image)["image"].unsqueeze(0).to(self.device)
        #                                  ^^^^^^^^^^^ add batch dimension:
        #                                  (3,512,512) → (1,3,512,512)

        # ── forward pass ──
        # inference_mode is a stricter, faster no_grad: it also disables
        # autograd's version-counter bookkeeping. Without it, PyTorch builds
        # a gradient graph for a backward pass that never happens — wasted
        # memory and time on every single request.
        with torch.inference_mode():
            logits = self.model(tensor)
            probs = torch.sigmoid(logits)      # raw logits → 0–1 probabilities

        # (1,1,512,512) → (512,512) numpy
        prob_map = probs.squeeze().cpu().numpy()
        mask = prob_map > self.threshold

        # ── post-process ──
        # Remove isolated specks. Applied AFTER thresholding, at 512×512.
        # ALTERNATIVE: binary_closing(mask, disk(3)) to bridge gaps in the
        # wrack line — cosmetic, makes masks look cleaner but slightly inflates
        # coverage and arguably papers over genuine model uncertainty. Skipped.
        mask = remove_small_objects(mask, min_size=MIN_BLOB_PIXELS)

        # ── coverage ──
        # DECISION: whole-frame fraction (Option A). Semantically weak — the
        # frame includes sky, ocean, buildings, and the pier crane, so this is
        # "% of all pixels", not "% of beach". Chosen anyway because:
        #   (a) scoring.py's bands were eyeballed from exactly these numbers,
        #       so the system is internally consistent today, and
        #   (b) the zone-relative version needs a hand-drawn sand polygon per
        #       camera, which is the next iteration, not this one.
        # When you switch to zone-relative, RECALIBRATE scoring.py's bands —
        # 1.5% of a sand zone is a completely different amount of seaweed than
        # 1.5% of the full frame.
        raw_coverage_pct = float(mask.mean() * 100.0)
        coverage_pct = raw_coverage_pct * COVERAGE_CALIBRATION

        score, label = sargassum_score(coverage_pct)

        # ── optional mask export for the UI overlay ──
        # DESIGN CHOICE: write a PNG and return the PATH, never the array.
        # A 512×512 boolean array serialized into a JSON response would bloat
        # every payload for a feature only the detail page uses. The frontend
        # requests the image separately if it wants the deck.gl overlay.
        mask_path = None
        if save_mask_to:
            Path(save_mask_to).parent.mkdir(parents=True, exist_ok=True)
            # Resize back to the ORIGINAL image dimensions so the overlay
            # aligns with the photo the user sees. INTER_NEAREST because this
            # is a binary mask — interpolation would invent grey edge pixels.
            full = cv2.resize(mask.astype(np.uint8),
                              (image.shape[1], image.shape[0]),
                              interpolation=cv2.INTER_NEAREST)

            # Write RGBA, not black-and-white. The only consumer of this file is
            # the detail page's cam feed, which stacks it over the photo — a
            # flat B/W PNG would hide the beach behind an opaque rectangle and
            # force the browser to do per-pixel work to key it out. Encoding
            # transparency here means the frontend is a plain <img> on top of
            # another <img>, no canvas.
            #
            # BGRA order: cv2.imwrite expects BGR(A), so this is a rust-orange
            # that reads clearly against wet sand without hiding what's beneath.
            h, w = full.shape
            rgba = np.zeros((h, w, 4), dtype=np.uint8)
            rgba[..., 0] = 40      # B
            rgba[..., 1] = 90      # G
            rgba[..., 2] = 210     # R
            rgba[..., 3] = full * 150   # alpha only where sargassum was detected
            cv2.imwrite(save_mask_to, rgba)
            mask_path = save_mask_to

        return {
            "coverage_pct": round(coverage_pct, 3),
            "raw_coverage_pct": round(raw_coverage_pct, 3),  # pre-calibration,
                                                             # kept for debugging
                                                             # and recalibration
            "score": round(score, 1),      # 0–10 sub-score for the index
            "label": label,                # Light / Mild / Moderate / Heavy
            "threshold": self.threshold,   # echoed so results are reproducible
            "mask_path": mask_path,
        }


# ─────────────────────────────────────────────────────────────────────────
# WATER SEVERITY (BETA)
# ─────────────────────────────────────────────────────────────────────────

class WaterSeverityClassifier:
    """ResNet34 binary classifier: low vs. elevated sargassum in the water.

    ⚠️ BETA — DATA-LIMITED. Trained on 51 images with only 8 'elevated'
    examples. It memorized the training set (train F1 → 1.0) but does not
    generalize (test F1 ≈ 0.5, catching 1 of 3 held-out positives).

    Shipped behind a beta flag deliberately: the PIPELINE is correct and will
    work once more moderate/heavy-water frames are labeled. Treat its output
    as a weak hint, not a measurement — which is exactly why scoring.py gives
    water the lowest weight of any signal.
    """

    def __init__(self, weights_path: str, device: str = "cpu"):
        import torchvision   # local import: only needed if this model is used

        self.device = device
        self.model = torchvision.models.resnet34(weights=None)  # same reasoning
        self.model.fc = torch.nn.Linear(self.model.fc.in_features, 2)
        self.model.load_state_dict(torch.load(weights_path, map_location=device))
        self.model.to(device)
        self.model.eval()

    def predict(self, image: np.ndarray) -> dict:
        """Returns a severity label plus the model's confidence.

        Confidence is surfaced because it's genuinely useful here: given how
        thin the training data is, a low-confidence 'elevated' call should be
        treated with more skepticism than a high-confidence one. The UI can
        use it to decide whether to show the reading at all.
        """
        tensor = _tf(image=image)["image"].unsqueeze(0).to(self.device)
        with torch.inference_mode():
            logits = self.model(tensor)
            probs = torch.softmax(logits, dim=1).squeeze().cpu().numpy()

        idx = int(probs.argmax())
        # Class 1 = 'elevated' (moderate/heavy collapsed during training).
        # Mapped back to scoring.py's vocabulary, conservatively: an
        # 'elevated' prediction becomes 'moderate' rather than 'heavy',
        # because the model has only 2 'heavy' examples total and cannot
        # meaningfully distinguish the two.
        label = "moderate" if idx == 1 else "light"

        return {
            "severity": label,
            "confidence": round(float(probs[idx]), 3),
            "beta": True,      # so the API response self-documents its own
                               # reliability rather than relying on the
                               # frontend to remember which signals are beta
        }


# ─────────────────────────────────────────────────────────────────────────
# CALIBRATION — how to actually derive COVERAGE_CALIBRATION
# ─────────────────────────────────────────────────────────────────────────

def fit_calibration(detector: "SargassumDetector",
                    image_mask_pairs: list[tuple[np.ndarray, np.ndarray]]) -> dict:
    """Fit the coverage calibration factor from labeled validation data.

    RUN THIS IN COLAB, not in production — it needs your ground-truth masks,
    which don't ship with the backend. Paste the resulting number into
    COVERAGE_CALIBRATION above (and then re-check scoring.py's bands).

    The factor is the ratio that best maps predicted coverage onto true
    coverage across the validation set. Using the ratio of SUMS rather than
    the mean of per-image ratios avoids letting near-zero-coverage images
    (where a tiny absolute error is a huge relative one) dominate the fit.

    Also reports correlation — which matters MORE than the factor itself.
    A scalar correction only makes sense if predicted coverage genuinely
    TRACKS true coverage (high correlation). If correlation is weak, no
    single multiplier will fix it, and you should leave calibration at 1.0.

    Args:
        image_mask_pairs: (rgb_image, ground_truth_binary_mask) from your
                          held-out validation days.
    """
    preds, truths = [], []
    for img, true_mask in image_mask_pairs:
        out = detector.predict(img)
        preds.append(out["raw_coverage_pct"])       # RAW — uncalibrated
        truths.append(float(true_mask.mean() * 100.0))

    preds_arr, truths_arr = np.array(preds), np.array(truths)
    correlation = float(np.corrcoef(preds_arr, truths_arr)[0, 1])
    factor = float(truths_arr.sum() / preds_arr.sum()) if preds_arr.sum() > 0 else 1.0

    return {
        "suggested_calibration": round(factor, 3),
        "correlation": round(correlation, 3),
        "n_images": len(preds),
        # Guidance, not a hard rule: below ~0.7 correlation the relationship
        # is too noisy for a scalar correction to be meaningful.
        "recommendation": ("apply the factor" if correlation > 0.7
                           else "correlation too weak — keep calibration at 1.0"),
    }

# ─────────────────────────────────────────────────────────────────────────
# CROWD COUNTING (BETA) — append to inference.py
# ─────────────────────────────────────────────────────────────────────────

class CrowdCounter:
    """Person detection via YOLO + SAHI sliced inference.

    ⚠️ BETA. Plain YOLO fails badly on this camera: people are only a handful
    of pixels at pier height, and it counted 2 where a dozen were visible.
    SAHI tiling recovers the mid-distance people (3→6, 2→14 on test frames)
    by running detection on 512px tiles, so a distant figure that was 5px in
    the full frame becomes ~30px within its tile.

    Still misses people near the vanishing point — there's genuinely no detail
    left to detect there. Treat the output as a DENSITY TIER, not a headcount;
    scoring.py already buckets it that way.

    ⚠️ SLOW: 20–40 forward passes per image. Call this at INGEST only, never
    inside a request handler.
    """

    def __init__(self,
                 model_path: str = "models/yolo26m.pt",   # was "yolo26m.pt"
                 confidence: float = 0.35,
                 device: str = "cpu"):
        """
        confidence=0.35, NOT the 0.15 used during testing. At 0.15 the tiled
        detector hallucinated badly — phantom "person 0.15–0.25" boxes stacked
        on the pier crane, plus spurious boats/surfboards/kites. 0.35 trades a
        few genuinely-distant people for far fewer false positives, which is
        the right trade when the output is a coarse density bucket anyway.

        yolo26m (medium), not nano: small-object recall is the entire problem
        here, and nano was measurably worse. Cost is speed — acceptable since
        this runs at ingest, not per-request.
        """
        # Local import: these are heavy packages, and this keeps `import
        # inference` cheap for callers that only need the sargassum model.
        from sahi import AutoDetectionModel
        from sahi.postprocess.backends import set_postprocess_backend

        # PIN THE NMS BACKEND. Do not let SAHI auto-detect this.
        #
        # THE BUG THIS FIXES, because it is worth recognising again elsewhere:
        # SAHI's resolve_backend() picks numba over numpy whenever
        # is_available("numba") is true — and that check only asks whether the
        # DISTRIBUTION is present, not whether it actually imports. On a machine
        # where numba is installed but incompatible with the installed numpy
        # (`Numba needs NumPy 2.1 or less. Got NumPy 2.4.` — exactly the state of
        # the system Anaconda environment here), SAHI selects numba, then throws
        # ImportError the first time it postprocesses a sliced prediction.
        #
        # The failure was invisible in the worst way. Construction succeeds, so
        # load_models() reports "crowd model loaded" and /  listed it as healthy;
        # the import only blows up later, inside count(), where run_ingest's
        # per-model try/except swallowed it. Every reading stored crowd_count =
        # NULL, which the UI renders as "—" — identical to a beach with no
        # camera. The people detector looked like it was returning nothing when
        # it was in fact never running.
        #
        # numpy is the right pin rather than a fallback-on-error: it is always
        # available, the counts here are 0-50 boxes per frame (numba's JIT wins
        # only on large prediction counts, and its first-call compile cost
        # exceeds any saving at this size), and this device is "cpu" so the
        # torchvision-on-CUDA path is unreachable anyway. Pinning also makes NMS
        # tie-breaking deterministic across environments, which matters for the
        # same reason INGEST_WIDTH is fixed in main.run_ingest: crowd counts are
        # compared BETWEEN beaches, so they must not depend on which machine
        # happened to run the model.
        set_postprocess_backend("numpy")

        self.confidence = confidence
        self.model = AutoDetectionModel.from_pretrained(
            model_type="ultralytics",
            model_path=model_path,
            confidence_threshold=confidence,
            device=device,
        )

    def count(self, image_path: str, save_boxes_to: Optional[str] = None) -> dict:
        """Count people in one image.

        Takes a PATH, not an array — SAHI's API reads the file itself, and
        forcing an array through would mean writing it back to a temp file.
        (Note the asymmetry with SargassumDetector, which takes an array.
        Not ideal, but fighting the library's interface here would be worse.)

        `save_boxes_to` optionally writes a transparent RGBA PNG with one box
        per detected person, for the cam feed overlay. Same contract as
        SargassumDetector.predict's save_mask_to: write a file, return the
        path, never ship pixel arrays through JSON.
        """
        from sahi.predict import get_sliced_prediction

        result = get_sliced_prediction(
            image_path, self.model,
            slice_height=512, slice_width=512,
            # 20% overlap so a person straddling a tile boundary isn't cut in
            # half and missed by both tiles. SAHI merges duplicate detections
            # across the overlap automatically.
            overlap_height_ratio=0.2,
            overlap_width_ratio=0.2,
            verbose=0,
        )

        # Filter to COCO class 0 = person. This alone discards the boat /
        # surfboard / kite false positives seen in testing — which is why the
        # crane-region exclusion mask I'd suggested turned out to be
        # unnecessary: the crane produced spurious *objects*, not spurious
        # *people*.
        people = [p for p in result.object_prediction_list
                  if p.category.id == 0]
        n = len(people)

        # Bucket immediately. DESIGN CHOICE: the raw count is not trustworthy
        # to ±1, but the tier is. Returning both lets the UI show the tier
        # while keeping the number available for debugging and recalibration.
        if n < 5:     tier = "Not Busy"
        elif n < 20:  tier = "Moderate"
        elif n < 50:  tier = "Busy"
        else:         tier = "Very Busy"

        boxes_path = None
        if save_boxes_to:
            boxes_path = self._draw_boxes(image_path, people, save_boxes_to)

        return {
            "people_count": n,
            "tier": tier,
            "confidence_threshold": self.confidence,
            "beta": True,
            "boxes_path": boxes_path,
        }

    @staticmethod
    def _draw_boxes(image_path: str, people: list, save_to: str) -> Optional[str]:
        """Transparent RGBA PNG with one rectangle per detected person.

        Sized to the SOURCE image so the frontend can stack it directly over
        the frame with no scaling maths. Detections at this camera are only a
        few pixels tall at distance, so boxes get a minimum size — a 3px
        rectangle is invisible next to a 1200px-wide photo, which would make
        the overlay look broken rather than empty.

        Failure here must not lose a good count: the caller already has `n`,
        so a drawing problem returns None instead of raising.
        """
        try:
            src = cv2.imread(image_path)
            if src is None:
                return None
            h, w = src.shape[:2]
            canvas = np.zeros((h, w, 4), dtype=np.uint8)

            for p in people:
                bb = p.bbox.to_xyxy()
                x1, y1, x2, y2 = (int(round(v)) for v in bb)
                # Enforce a visible minimum box, centred on the detection.
                if x2 - x1 < 10:
                    cx = (x1 + x2) // 2
                    x1, x2 = cx - 5, cx + 5
                if y2 - y1 < 10:
                    cy = (y1 + y2) // 2
                    y1, y2 = cy - 5, cy + 5
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(w - 1, x2), min(h - 1, y2)
                # BGRA — cyan reads clearly against sand, sea and sky alike,
                # and is distinct from the sargassum overlay's orange so both
                # can be shown at once without ambiguity.
                cv2.rectangle(canvas, (x1, y1), (x2, y2), (255, 240, 60, 255), 2)

            Path(save_to).parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(save_to, canvas)
            return save_to
        except Exception as e:
            print(f"[inference] crowd box overlay failed: {e}")
            return None
    
if __name__ == "__main__":
    # Smoke test:  python inference.py <image_path> [sargassum|water|crowd|all]
    #
    # Tests each model in ISOLATION, before FastAPI is anywhere near the
    # picture. Each is wrapped in its own try/except so a missing weights file
    # or a broken dependency in one model doesn't hide whether the others work.
    import sys, time

    if len(sys.argv) < 2:
        print("usage: python inference.py <image_path> [sargassum|water|crowd|all]")
        sys.exit(1)

    image_path = sys.argv[1]
    which = sys.argv[2] if len(sys.argv) > 2 else "all"

    # Load once, share across models. cv2.imread returns BGR; every model here
    # expects RGB, so convert exactly once at the boundary rather than inside
    # each predict() — same discipline as the training pipeline.
    bgr = cv2.imread(image_path)
    if bgr is None:
        print(f"could not read {image_path}")
        sys.exit(1)
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    print(f"loaded {image_path}  shape={rgb.shape}\n")

    # ── Sargassum segmentation (the main model) ──
    if which in ("sargassum", "all"):
        try:
            t = time.time()
            det = SargassumDetector("models/best.pt")
            out = det.predict(rgb, save_mask_to="/tmp/mask_preview.png")
            print(f"[sargassum]  ({time.time()-t:.2f}s)")
            for k, v in out.items():
                print(f"    {k:20s} {v}")
            print("    → mask written to /tmp/mask_preview.png (open it to "
                  "confirm it lands on real sargassum)\n")
        except Exception as e:
            print(f"[sargassum]  FAILED: {e}\n")

    # ── Water severity (beta) ──
    if which in ("water", "all"):
        try:
            t = time.time()
            water = WaterSeverityClassifier("models/water_best.pt")
            out = water.predict(rgb)
            print(f"[water]      ({time.time()-t:.2f}s)")
            for k, v in out.items():
                print(f"    {k:20s} {v}")
            # Reminder at the point of use, not buried in a docstring: this
            # model caught 1 of 3 held-out positives. Don't over-read it.
            print("    → BETA: data-limited (8 positive examples). Weak hint "
                  "only.\n")
        except Exception as e:
            print(f"[water]      FAILED: {e}\n")

    # ── Crowd counting (beta, SLOW) ──
    if which in ("crowd", "all"):
        try:
            print("[crowd]      running sliced inference (20-40 passes, "
                  "expect several seconds)...")
            t = time.time()
            # Takes the PATH, not the array — SAHI reads the file itself.
            crowd = CrowdCounter(model_path="models/yolo26m.pt")
            out = crowd.count(image_path)
            elapsed = time.time() - t
            print(f"[crowd]      ({elapsed:.2f}s)")
            for k, v in out.items():
                print(f"    {k:20s} {v}")
            # This timing IS the architectural argument: if it's slow here,
            # it would be slow in a request handler too. Hence: ingest-time only.
            if elapsed > 1.0:
                print(f"    → {elapsed:.1f}s confirms this must run at INGEST, "
                      "never inside a request handler.\n")
        except Exception as e:
            print(f"[crowd]      FAILED: {e}\n")