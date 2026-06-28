import yaml
import os

class Config:
    def __init__(self, config_file="config.yaml"):
        self.config_path = config_file
        self.data = self._load()

    def _load(self):
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        with open(self.config_path, "r") as f:
            return yaml.safe_load(f)

    @property
    def symbols(self):
        return self.data.get("symbols", [])

    @property
    def risk(self):
        return self.data.get("risk", {})

    @property
    def setup(self):
        return self.data.get("setup", {})

    @property
    def stops(self):
        return self.data.get("stops", {})

    @property
    def execution(self):
        return self.data.get("execution", {})

config = Config()
