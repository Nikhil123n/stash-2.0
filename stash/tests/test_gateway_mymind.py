import asyncio
import base64
import json
import time

import httpx
import jwt
import pytest

from stash.config import StashConfig
from stash.gateway.mymind import (
    AuthError,
    MyMindGateway,
    RateLimitError,
    _parse_rate_limit_headers,
)


def _make_config() -> StashConfig:
    secret = base64.b64encode(b"test-secret-key-32bytes-long!!!!").decode()
    return StashConfig(
        DISCORD_TOKEN="test",
        STASH_OWNER_ID=123,
        STASH_ENV="production",
        MYMIND_KID="test-kid-123",
        MYMIND_SECRET=secret,
        GOOGLE_APPLICATION_CREDENTIALS="/tmp/k.json",
        GROQ_API_KEY="groq",
    )


class TestJwtSigning:
    def test_jwt_structure(self):
        config = _make_config()
        gw = MyMindGateway(config)

        token = gw._sign_request("GET", "/spaces")

        header = jwt.get_unverified_header(token)
        assert header["alg"] == "HS256"
        assert header["kid"] == "test-kid-123"

        payload = jwt.decode(token, base64.b64decode(config.MYMIND_SECRET), algorithms=["HS256"])
        assert payload["method"] == "GET"
        assert payload["path"] == "/spaces"
        assert "iat" in payload
        assert "exp" in payload
        assert payload["exp"] - payload["iat"] == 300

    def test_jwt_different_per_request(self):
        config = _make_config()
        gw = MyMindGateway(config)

        token1 = gw._sign_request("GET", "/spaces")
        token2 = gw._sign_request("POST", "/objects")

        p1 = jwt.decode(token1, base64.b64decode(config.MYMIND_SECRET), algorithms=["HS256"])
        p2 = jwt.decode(token2, base64.b64decode(config.MYMIND_SECRET), algorithms=["HS256"])
        assert p1["method"] == "GET"
        assert p2["method"] == "POST"
        assert p1["path"] == "/spaces"
        assert p2["path"] == "/objects"


class TestAuthError:
    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self, httpx_mock):
        httpx_mock.add_response(url="https://api.mymind.com/spaces", status_code=401)

        config = _make_config()
        gw = MyMindGateway(config)

        with pytest.raises(AuthError, match="mymind auth failed"):
            await gw._request("GET", "/spaces")

    @pytest.mark.asyncio
    async def test_403_raises_auth_error(self, httpx_mock):
        httpx_mock.add_response(url="https://api.mymind.com/objects", status_code=403)

        config = _make_config()
        gw = MyMindGateway(config)

        with pytest.raises(AuthError, match="mymind auth failed"):
            await gw._request("POST", "/objects")


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_429_retries_with_header_backoff(self, httpx_mock):
        httpx_mock.add_response(
            url="https://api.mymind.com/tags",
            status_code=429,
            headers={"RateLimit": "r=0, t=1, w=300;policy=burst"},
        )
        httpx_mock.add_response(
            url="https://api.mymind.com/tags",
            json=[{"name": "python"}, {"name": "ai"}],
        )

        config = _make_config()
        gw = MyMindGateway(config)

        tags = await gw.get_tags()
        assert tags == ["python", "ai"]

    @pytest.mark.asyncio
    async def test_429_twice_raises_rate_limit_error(self, httpx_mock):
        httpx_mock.add_response(url="https://api.mymind.com/tags", status_code=429)
        httpx_mock.add_response(url="https://api.mymind.com/tags", status_code=429)

        config = _make_config()
        gw = MyMindGateway(config)

        with pytest.raises(RateLimitError):
            await gw.get_tags()


class TestRateLimitHeaderParsing:
    def test_parse_cost(self):
        resp = httpx.Response(200, headers={"RateLimit-Cost": "10"})
        info = _parse_rate_limit_headers(resp)
        assert info["cost"] == 10

    def test_parse_remaining(self):
        resp = httpx.Response(200, headers={
            "RateLimit": "r=500, t=300, w=300;policy=burst, r=9000, t=2592000, w=2592000;policy=sustained",
        })
        info = _parse_rate_limit_headers(resp)
        assert info["burst_remaining"] == 500
        assert info["sustained_remaining"] == 9000

    def test_parse_retry_after_on_exhausted(self):
        resp = httpx.Response(429, headers={
            "RateLimit": "r=0, t=45, w=300;policy=burst",
        })
        info = _parse_rate_limit_headers(resp)
        assert info["retry_after"] == 45
