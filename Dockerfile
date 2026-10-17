FROM python:3.12-alpine

WORKDIR /app
COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8767

CMD ["python", "server.py"]
