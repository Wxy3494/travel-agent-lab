FROM python:3.12-slim
WORKDIR /workspace
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml .
COPY app ./app
COPY .streamlit ./.streamlit
RUN pip install --no-deps . && useradd --create-home traveler && mkdir -p data/runtime && chown -R traveler:traveler /workspace
USER traveler
EXPOSE 8000 8501
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
