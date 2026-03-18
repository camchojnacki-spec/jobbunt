FROM python:3.11-slim
WORKDIR /app
RUN mkdir -p /app/data/uploads
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
ENV ENV=production
EXPOSE 8080
CMD ["python", "run.py"]
