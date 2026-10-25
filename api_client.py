"""
api_client.py: the only file that talks to the IFB220 Azure endpoints.

It reads the API key from the API_KEY environment variable and offers two
methods: chat() for GPT-4.1-mini and embed() for text-embedding-3-small.
Temporary failures (rate limits, timeouts, server errors) are retried with a
growing wait. Permanent ones (bad key, bad request) fail straight away. Token
use and call counts are kept for the session, and every call is passed to an
optional callback so the logger can record it.
"""

import os
import time

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    AzureOpenAI,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)

# Load a local .env file if python-dotenv is installed. This only helps during
# development; the program just needs API_KEY to be set in the environment.
try:
    from dotenv import load_dotenv

    # Look next to this file first, then in the current directory. A variable
    # already set in the real environment is never overwritten.
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)
    load_dotenv(".env", override=False)
except ImportError:
    pass


class AIServiceError(Exception):
    """Raised when the AI service cannot be used (after retries, or a permanent error)."""


class ContentFilteredError(AIServiceError):
    """Raised when the service's own content filter rejects a prompt."""


class AIClient:
    def __init__(self, config, event_callback=None):
        """
        config: the full config.json dictionary.
        event_callback: optional function called with a dict after every API
        call, successful or not, so the logger can record it.
        """
        api_cfg = config["api"]
        key_var = api_cfg.get("api_key_env_var", "API_KEY")
        api_key = os.environ.get(key_var, "").strip()
        if not api_key:
            raise AIServiceError(
                f"Environment variable {key_var} is not set. "
                f"Set it to your IFB220 API key before running."
            )

        self.chat_model = api_cfg["chat_model"]
        self.embedding_model = api_cfg["embedding_model"]
        self.max_retries = api_cfg.get("max_retries", 3)
        self.backoff = api_cfg.get("retry_backoff_seconds", 2)
        self.event_callback = event_callback

        # max_retries=0 turns off the SDK's built-in retrying so that the
        # retry policy below is the only one (and every attempt is logged).
        self._client = AzureOpenAI(
            base_url=api_cfg["base_url"],
            api_key=api_key,
            api_version=api_cfg["api_version"],
            timeout=api_cfg.get("timeout_seconds", 30),
            max_retries=0,
        )

        # Cumulative usage for the whole session (reported at exit).
        self.usage = {
            "chat_calls": 0,
            "embedding_calls": 0,
            "failed_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "embedding_tokens": 0,
        }

    # Public helpers
    def chat(self, messages, temperature=0.5, max_tokens=500, purpose="chat"):
        """
        Send a chat completion request and return the reply text.
        purpose is a label such as 'chat', 'classifier' or 'summary'; it shows
        in the logs which part of the program made the call.
        """
        def call():
            return self._client.chat.completions.create(
                model=self.chat_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        response, latency = self._with_retries(call, kind="chat", purpose=purpose)

        usage = response.usage
        prompt_t = usage.prompt_tokens if usage else 0
        completion_t = usage.completion_tokens if usage else 0
        self.usage["chat_calls"] += 1
        self.usage["prompt_tokens"] += prompt_t
        self.usage["completion_tokens"] += completion_t
        self._emit(
            "api_call",
            kind="chat",
            purpose=purpose,
            prompt_tokens=prompt_t,
            completion_tokens=completion_t,
            latency_s=round(latency, 3),
        )

        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        return text

    def embed(self, texts, purpose="embedding"):
        """
        Embed one string or a list of strings.
        Returns a list of vectors (list of list of floats), in input order.
        """
        if isinstance(texts, str):
            texts = [texts]

        def call():
            return self._client.embeddings.create(
                model=self.embedding_model, input=texts
            )

        response, latency = self._with_retries(call, kind="embedding", purpose=purpose)

        tokens = response.usage.total_tokens if response.usage else 0
        self.usage["embedding_calls"] += 1
        self.usage["embedding_tokens"] += tokens
        self._emit(
            "api_call",
            kind="embedding",
            purpose=purpose,
            embedding_tokens=tokens,
            n_inputs=len(texts),
            latency_s=round(latency, 3),
        )

        # The API returns items with an 'index'; sort to guarantee input order.
        ordered = sorted(response.data, key=lambda item: item.index)
        return [item.embedding for item in ordered]

    # Internals
    def _with_retries(self, call, kind, purpose):
        """
        Run call() with retries on transient errors.
        Returns (response, latency_of_successful_attempt).
        """
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            start = time.time()
            try:
                response = call()
                return response, time.time() - start

            # Permanent errors: retrying cannot help
            except AuthenticationError as e:
                self._fail(kind, purpose, "authentication", attempt, e)
                raise AIServiceError(
                    "Authentication failed. Check that API_KEY is correct."
                ) from e

            except BadRequestError as e:
                # Azure returns 400 with code 'content_filter' when its own
                # safety filter blocks the prompt.
                code = getattr(e, "code", None) or ""
                self._fail(kind, purpose, f"bad_request:{code}", attempt, e)
                if code == "content_filter" or "content_filter" in str(e):
                    raise ContentFilteredError(
                        "The request was rejected by the service content filter."
                    ) from e
                raise AIServiceError(f"The service rejected the request: {e}") from e

            # Temporary errors: wait and try again
            except (RateLimitError, APITimeoutError, APIConnectionError,
                    InternalServerError) as e:
                last_error = e
                self._fail(kind, purpose, type(e).__name__, attempt, e)

            except APIStatusError as e:
                # Any other HTTP error: retry only if it looks server-side.
                last_error = e
                self._fail(kind, purpose, f"http_{e.status_code}", attempt, e)
                if e.status_code < 500 and e.status_code != 429:
                    raise AIServiceError(f"Unexpected API error: {e}") from e

            if attempt < self.max_retries:
                time.sleep(self.backoff * (2 ** (attempt - 1)))  # 2 s after the first failure, 4 s after the second

        raise AIServiceError(
            f"The AI service is unavailable after {self.max_retries} attempts."
        ) from last_error

    def _fail(self, kind, purpose, error_type, attempt, error):
        self.usage["failed_calls"] += 1
        self._emit(
            "api_error",
            kind=kind,
            purpose=purpose,
            error_type=error_type,
            attempt=attempt,
            detail=str(error)[:200],
        )

    def _emit(self, event_type, **fields):
        if self.event_callback:
            self.event_callback({"event": event_type, **fields})