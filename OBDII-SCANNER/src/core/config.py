import yaml
import os

class AppConfig:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(AppConfig, cls).__new__(cls)
            cls._instance._config = {}
        return cls._instance
        
    def load(self, config_path: str, overrides: dict = None):
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                self._config = yaml.safe_load(f) or {}
        
        if overrides:
            for k, v in overrides.items():
                if v is not None:
                    self.set(k, v)
                    
    def get(self, key: str, default=None):
        keys = key.split('.')
        val = self._config
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                return default
        return val
        
    def set(self, key: str, value):
        keys = key.split('.')
        d = self._config
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value
