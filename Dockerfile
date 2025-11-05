# Usa imagem Python slim (leve e estável)
FROM python:3.10-slim

# Instala o Tesseract OCR e dependências do sistema
RUN apt-get update && \
    apt-get install -y tesseract-ocr libtesseract-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Define diretório de trabalho
WORKDIR /app

# Copia o código do projeto
COPY . /app

# Instala dependências Python
RUN pip install --no-cache-dir -r requirements.txt

# Exponha a porta do Flask
EXPOSE 3000

# Comando padrão para iniciar a aplicação
CMD ["python", "financeiro.py"]
