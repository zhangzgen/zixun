import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    archive_dir: Path
    database_path: Path
    static_dir: Path
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    max_rounds: int = 6
    max_tool_calls: int = 12
    timeout: float = 150
    report_archive_dir: Path | None = None

    @property
    def model_ready(self):
        return bool(self.api_key and self.model)

    @classmethod
    def from_env(cls):
        return cls(
            archive_dir=Path(os.getenv("ARCHIVE_DIR", str(ROOT / "ai_news_daily"))),
            database_path=Path(os.getenv("NEWS_DB_PATH", str(ROOT / ".news_web" / "index.sqlite3"))),
            static_dir=ROOT / "web" / "dist",
            api_key=os.getenv("OPENAI_API_KEY", ""),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            model=os.getenv("OPENAI_MODEL", ""),
            report_archive_dir=Path(os.getenv("REPORT_ARCHIVE_DIR", str(ROOT / "technical_report_daily"))),
        )
