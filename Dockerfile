FROM python:3.11-slim

# Встановлення ffmpeg для зведення аудіо та відео
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Кешування та встановлення Python залежностей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копіювання коду проєкту
COPY . .

# Запуск бота у фоновому режимі
CMD ["python", "main.py"]
