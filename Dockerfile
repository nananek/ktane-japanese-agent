FROM python:3.12-slim

# ffmpeg: 読み上げ音声の再生 / libopus0: Discordの音声の送受信
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# torchはsilero-vad (CPUで十分) にしか使わないため、数GBあるCUDA版ではなくCPU版を先に入れておく。
# faster-whisper のGPU推論に要るCUDA 12ライブラリは requirements.txt の nvidia-* パッケージが持ち込み、
# ドライバはホストの NVIDIA Container Toolkit から渡される
COPY requirements.txt .
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt

COPY . .

# マニュアルの書き起こし (knowledge/) は非公開のため、イメージには含めず実行時に /app/knowledge へマウントする
CMD ["python", "bot.py"]
