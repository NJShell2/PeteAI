from typing import List, Dict, Any, Optional
from openai import AsyncOpenAI
import httpx

from app.config import load_settings, get_api_key
from app.console import safe_print

class LLMClient:
    def __init__(self):
        pass

    def get_client(self, api_key: Optional[str] = None, base_url: Optional[str] = None) -> AsyncOpenAI:
        settings = load_settings()
        key = api_key or get_api_key(settings)
        if not key:
            raise RuntimeError(
                "No Purdue GenAI Studio API key is configured. Open Settings, "
                "paste your key, and test the connection before chatting."
            )
        url = base_url or settings.purdue_api_url
        
        # Open WebUI endpoint requires trailing slash or /v1 depending on setup
        if not url.endswith("/"):
            url = url + "/"
        if not url.endswith("v1/"):
            url = url + "v1/"

        return AsyncOpenAI(
            api_key=key,
            base_url=url,
            timeout=120.0,
            http_client=httpx.AsyncClient(verify=False) # In case Purdue RCAC self-signed/internal certs
        )

    async def list_models(self, api_key: Optional[str] = None,
                          base_url: Optional[str] = None,
                          strict: bool = False) -> List[Dict[str, Any]]:
        """List models from Studio.

        ``strict=True`` raises on any failure (used by the connection test);
        otherwise a stale-but-useful offline fallback list is returned so the
        UI still has models to offer during a network outage.
        """
        settings = load_settings()
        client = self.get_client(api_key=api_key, base_url=base_url)
        try:
            response = await client.models.list()
            models = []
            for m in response.data:
                models.append({
                    "id": m.id,
                    "name": getattr(m, "name", m.id) or m.id,
                    "owned_by": getattr(m, "owned_by", "purdue")
                })
            return models
        except Exception as e:
            safe_print(f"Failed to fetch models from Purdue GenAI Studio: {e}")
            if strict:
                raise
            # Offline fallback: Purdue GenAI Studio models (verified live model IDs)
            return [
                {"id": "gemma4:26b-a4b", "name": "Gemma 4 26B (A4B)", "owned_by": "purdue"},
                {"id": "llama3.3:70b", "name": "Llama 3.3 70B", "owned_by": "purdue"},
                {"id": "llama4:latest", "name": "Llama 4", "owned_by": "purdue"},
                {"id": "qwen3:32b", "name": "Qwen 3 32B", "owned_by": "purdue"},
                {"id": "qwen3-coder:latest", "name": "Qwen 3 Coder", "owned_by": "purdue"},
                {"id": "gpt-oss:120b", "name": "GPT-OSS 120B", "owned_by": "purdue"},
                {"id": "deepseek-r1:32b", "name": "DeepSeek R1 32B", "owned_by": "purdue"},
                {"id": "mistral:latest", "name": "Mistral (Latest)", "owned_by": "purdue"}
            ]


llm_client = LLMClient()
