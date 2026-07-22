FROM reignz3/wzmlhk:wzadv-3.1.10

WORKDIR /usr/src/app

COPY requirements.txt .
RUN uv pip install --python /wzvenv/bin/python --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["bash", "start.sh"]
