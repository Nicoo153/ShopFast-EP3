import os, json, time, sqlite3, logging
from datetime import datetime, timezone
import pika

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("shopfast-worker")

RABBIT_HOST = os.getenv("RABBIT_HOST", "rabbitmq")
DB_PATH = os.getenv("DB_PATH", "/data/analytics.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_events (
            event_id TEXT PRIMARY KEY,
            occurred_at TEXT,
            processed_at TEXT,
            tipo TEXT,
            request_id TEXT,
            model_version TEXT,
            modo TEXT,
            payload_json TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS delivery_stats (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            total_deliveries INTEGER DEFAULT 0,
            duplicate_deliveries INTEGER DEFAULT 0
        )
    """)
    conn.execute("INSERT OR IGNORE INTO delivery_stats (id, total_deliveries, duplicate_deliveries) VALUES (1, 0, 0)")
    conn.commit()
    return conn

def process_message(conn, body: bytes):
    data = json.loads(body.decode("utf-8"))
    event_id = data["event_id"]
    cur = conn.cursor()
    cur.execute("UPDATE delivery_stats SET total_deliveries = total_deliveries + 1 WHERE id = 1")
    # Inserción idempotente (INSERT OR IGNORE): evita doble conteo ante reentregas antes del ACK
    cur.execute("""
        INSERT OR IGNORE INTO processed_events
        (event_id, occurred_at, processed_at, tipo, request_id, model_version, modo, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        event_id,
        data.get("occurred_at"),
        datetime.now(timezone.utc).isoformat(),
        data.get("tipo", "unknown"),
        data.get("request_id"),
        data.get("model_version"),
        data.get("modo"),
        json.dumps(data)
    ))
    if cur.rowcount == 0:
        cur.execute("UPDATE delivery_stats SET duplicate_deliveries = duplicate_deliveries + 1 WHERE id = 1")
        logger.info(f"Duplicate event ignored idempotently: {event_id}")
    else:
        logger.info(f"Persisted unique event: {event_id}")
    conn.commit()

def main():
    conn = init_db()
    while True:
        try:
            params = pika.ConnectionParameters(host=RABBIT_HOST)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.queue_declare(queue="shopfast.events", durable=True)
            channel.basic_qos(prefetch_count=10)

            def callback(ch, method, properties, body):
                process_message(conn, body)
                # ACK explícito solo después de confirmar persistencia en disco
                ch.basic_ack(delivery_tag=method.delivery_tag)

            channel.basic_consume(queue="shopfast.events", on_message_callback=callback)
            logger.info("Worker listening on shopfast.events...")
            channel.start_consuming()
        except Exception as e:
            logger.warning(f"Waiting for RabbitMQ ({e})...")
            time.sleep(3)

if __name__ == "__main__":
    main()
