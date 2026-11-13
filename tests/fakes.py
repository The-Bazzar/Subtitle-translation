class FakeLLM:
    provider = "fake"
    batch_size = 10

    def model_name(self):
        return "test-model"

    def cfg(self):
        return {}
