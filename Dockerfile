FROM python:3.12-slim

WORKDIR /app

# Vendor directory contains all required Python wheels pre-downloaded
# so the image build does not require internet access to PyPI.
# Strategy: pip install --no-index --find-links=/app/vendor -r requirements.txt
COPY vendor/ vendor/
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/app/vendor -r requirements.txt

COPY src/ src/
COPY scripts/ scripts/
COPY fixtures.json .

ENV VERDICT_LEDGER_DB=/data/verdict_ledger.db
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python3", "src/backend/app.py"]
