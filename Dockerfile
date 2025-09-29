FROM python:3.11-slim

# Carpeta de trabajo dentro del contenedor
WORKDIR /app

# Copiar requirements e instalar dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código
COPY . .

# Evitar que Python use buffering (logs en tiempo real)
ENV PYTHONUNBUFFERED=1

# Comando para iniciar el bot
CMD ["python", "mogolibot.py"]
