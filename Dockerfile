# Temel imaj: Python 3.11 + Debian tabanlı (slim)
FROM python:3.11-slim

# Sistem paketlerini yükle
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    && apt-get clean

# Çalışma dizini oluştur
WORKDIR /app

# Tüm proje dosyalarını konteynıra kopyala
COPY . .

# Python bağımlılıklarını yükle
RUN pip install --upgrade pip && pip install -r requirements.txt

# Başlangıç komutu
CMD ["python", "main.py"]
