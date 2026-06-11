from pathlib import Path

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class PersonaConfig(BaseModel):
    name: str
    port: int


class Config(BaseModel):
    personas: list[PersonaConfig]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SHADOWBOX_")

    home: Path = Path("~/.shadowbox")

    @property
    def home_dir(self) -> Path:
        return self.home.expanduser()

    @property
    def config_file(self) -> Path:
        return self.home_dir / "config.yaml"

    @property
    def db_file(self) -> Path:
        return self.home_dir / "shadowbox.db"

    @property
    def keys_dir(self) -> Path:
        return self.home_dir / "keys"

    @property
    def initialized(self) -> bool:
        return self.config_file.exists()


def load_config(settings: Settings) -> Config:
    return Config.model_validate(yaml.safe_load(settings.config_file.read_text()))


def save_config(settings: Settings, config: Config) -> None:
    settings.config_file.write_text(
        yaml.safe_dump(config.model_dump(), sort_keys=False)
    )
