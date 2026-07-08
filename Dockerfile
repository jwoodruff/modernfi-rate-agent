FROM python:3.12-slim

WORKDIR /app

# Copy only the dependency file first so Docker can cache this layer —
# it only gets invalidated (and re-run) when requirements.txt actually changes,
# not every time you edit app code.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the rest of the application code.
COPY . .

EXPOSE 8000

CMD ["fastapi", "run", "app/main.py", "--host", "0.0.0.0", "--port", "8000"]