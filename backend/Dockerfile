#contains a sequential script of instructions used to automate the creation of a Docker container image
#a recipe/blueprint

#import the full python image
FROM python:3.11

#copy requirements.txt to here, note . relates to the image not the current directory in backend
COPY requirements.txt .
#install BEFORE we copy everything so as long as requirements.txt stays the same we run the application faster
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

#Single-stage: simple, one FROM, everything in one image. No need for docker compose ---

COPY models/ ./models/
#we hsot command
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]