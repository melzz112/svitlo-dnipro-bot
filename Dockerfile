# Код бота поверх базового образу (Dockerfile.base: Python, Chromium, залежності) — збирається без мережі.
# Перший раз локально: docker build -f Dockerfile.base -t svitlo-dnipro-bot-base:latest .
ARG BASE=svitlo-dnipro-bot-base:latest
FROM ${BASE}
WORKDIR /app
COPY *.py .
COPY scripts/ scripts/
CMD ["python", "bot.py"]
