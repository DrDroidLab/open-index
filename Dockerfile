FROM python:3.12-slim

WORKDIR /app
COPY . .

RUN pip install --break-system-packages -e .

CMD ["sh", "-c", "droid-brain seed-demo && streamlit run app.py --server.address 0.0.0.0 --server.port 8501"]
