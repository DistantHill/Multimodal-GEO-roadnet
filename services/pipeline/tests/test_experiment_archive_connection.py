from dataclasses import asdict

from pipeline.experiments import archive
from track.v2.config import PostgresConfig


def test_archive_connections_accept_actual_postgres_config(monkeypatch):
    config = PostgresConfig(host="postgres",port=5432,database="roadnet_eval",user="roadnet",
        password="test",connect_timeout=10,sslmode="prefer",keepalives=True,
        keepalives_idle=60,keepalives_interval=10,keepalives_count=5,tcp_user_timeout=30000)
    calls=[]
    class Result:
        def fetchone(self):
            return (1,)
    class Connection:
        def __enter__(self):
            return self
        def __exit__(self,*args):
            pass
        def execute(self,*args):
            return Result()
    def connect(**settings):
        calls.append(settings)
        assert type(settings["keepalives"]) is int
        assert settings["keepalives"] == 1
        return Connection()
    monkeypatch.setattr(archive.psycopg,"connect",connect)
    with archive.connect_archive(asdict(config)):
        pass
    archive.ensure_archive_database(asdict(config))
    assert [call["dbname"] for call in calls] == ["roadnet_eval","postgres"]
