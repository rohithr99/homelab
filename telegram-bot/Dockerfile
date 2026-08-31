#python base image - small
FROM python:3.12-slim

WORKDIR /app

#Install dependencies first (cached separately from code changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy the actual bot code
COPY bot.py .

CMD ["python", "bot.py"]
