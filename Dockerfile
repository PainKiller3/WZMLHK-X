FROM reignz3/wzmlhk:latest

WORKDIR /usr/src/app

COPY requirements.txt .
RUN uv pip install --python /wzvenv/bin/python --no-cache-dir -r requirements.txt

# Install rclone
RUN apt-get update && \
    apt-get install -y curl unzip && \
    curl https://rclone.org/install.sh | bash && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY . .

ENTRYPOINT ["bash", "start.sh"]
