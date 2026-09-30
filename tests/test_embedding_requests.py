import json
import unittest

import httpx
from openai import InternalServerError, OpenAI

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
