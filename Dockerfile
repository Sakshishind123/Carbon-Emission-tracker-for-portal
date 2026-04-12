# FROM python:3.8-slim-buster

# WORKDIR /python-docker

# COPY requirements.txt requirements.txt
# RUN pip3 install -r requirements.txt

# COPY . .

# CMD [ "python3", "-m" , "flask", "run", "--host=0.0.0.0"]







# Use an official Python runtime as a parent image
FROM python:3.9

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . .

# Install any dependencies from requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Expose the port that Flask runs on
EXPOSE 5000

# Set environment variables for Flask
ENV FLASK_APP=app.py
ENV FLASK_ENV=development

# Run the application
CMD ["python", "app.py"]
