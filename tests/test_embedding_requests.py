import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from openai import InternalServerError, OpenAI

import translate_srt as t
from subtitle_translation import model_context
from subtitle_translation.artifacts import atomic_write_json, read_json_object
from translate_srt import SingleInputEmbeddings


class SingleInputEmbeddingTests(unittest.TestCase):
    def client(self, handler):
        client = OpenAI(
            base_url="https://embedding.example.invalid/v1",
            api_key="test-key",
            default_headers={"X-Test-Route": "vertex"},
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        self.addCleanup(client.close)
        return client

    def test_documents_and_queries_send_scalar_text_for_all_models(self):
        for model in ("gemini-embedding-2", "text-embedding-3-small", "qwen3-embedding"):
            with self.subTest(model=model):
                inputs = []

                def handler(request):
                    payload = json.loads(request.content)
                    self.assertEqual(request.url.path, "/v1/embeddings")
                    self.assertEqual(request.headers["authorization"], "Bearer test-key")
                    self.assertEqual(request.headers["X-Test-Route"], "vertex")
                    self.assertEqual(payload["model"], model)
                    self.assertEqual(payload["encoding_format"], "float")
                    self.assertIsInstance(payload["input"], str)
                    inputs.append(payload["input"])
                    return httpx.Response(200, json={
                        "data": [{"index": 0, "embedding": [float(len(inputs)), 0.5]}],
                        "model": model,
                        "object": "list",
                    })

                embeddings = SingleInputEmbeddings(self.client(handler), model)
                self.assertEqual(embeddings.embed_documents([]), [])
                self.assertEqual(inputs, [])
                texts = ["first\nline", "second", "first\nline"]
                self.assertEqual(
                    embeddings.embed_documents(texts),
                    [[1.0, 0.5], [2.0, 0.5], [3.0, 0.5]],
                )
                self.assertEqual(embeddings.embed_query("query"), [4.0, 0.5])
                self.assertEqual(inputs, texts + ["query"])

    def test_failed_input_stops_processing_and_propagates_error(self):
        inputs = []

        def handler(request):
            text = json.loads(request.content)["input"]
            inputs.append(text)
            if text == "bad":
                return httpx.Response(503, json={"error": {"message": "service unavailable"}})
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5]}]})

        embeddings = SingleInputEmbeddings(self.client(handler), "gemini-embedding-2")
        with self.assertRaises(InternalServerError):
            embeddings.embed_documents(["first", "bad", "last"])
        self.assertEqual(inputs, ["first", "bad"])

    def test_only_used_embedding_model_is_backfilled_without_changing_request(self):
        def handler(request):
            self.assertEqual(json.loads(request.content), {
                "input": "one query", "model": "relay/embedding", "encoding_format": "float",
            })
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5]}]})

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "providers.json"
            atomic_write_json(path, {"relay": {"url": "https://fixture.invalid/v1",
                                               "default_model": "unused", "env_key": "FIXTURE_KEY"}})
            client = self.client(handler)
            with patch.object(t, "_config_root", return_value=root), patch.object(t, "OpenAI", return_value=client), \
                 patch.object(model_context, "_public_limits", return_value={
                     "embedding": 8192, "unused": 4096}) as catalogue:
                config = t.EmbeddingConfig(provider="relay", model="relay/embedding")
                embeddings = t.embedding_function(config, {"FIXTURE_KEY": "fixture"})
                self.assertEqual(embeddings.embed_documents([]), [])
                catalogue.assert_not_called()
                self.assertEqual(embeddings.embed_query("one query"), [0.5])
                self.assertEqual(embeddings.embed_query("one query"), [0.5])
                catalogue.assert_called_once()
            entry, = read_json_object(path)["relay"]["model_info"]
        self.assertEqual((entry["id"], entry["context_length"]), ("relay/embedding", 8192))
        self.assertEqual(entry["context_length_source"], "openrouter")

    def test_single_input_requires_exactly_one_response_vector(self):
        for count in (0, 2):
            with self.subTest(count=count):
                def handler(request):
                    return httpx.Response(200, json={
                        "data": [{"index": index, "embedding": [0.5]} for index in range(count)],
                    })

                embeddings = SingleInputEmbeddings(self.client(handler), "test-model")
                with self.assertRaisesRegex(ValueError, "exactly one vector"):
                    embeddings.embed_query("text")


if __name__ == "__main__":
    unittest.main()
