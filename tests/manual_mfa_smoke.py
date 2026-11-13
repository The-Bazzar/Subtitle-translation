"""Real local MFA integration; translation responses are schema fixtures, not a quality benchmark."""

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import translate_srt as t
from subtitle_translation import mfa
from subtitle_translation.mfa_setup import wsl_host_path


class FixtureLLM:
    provider = "fixture"
    batch_size = 8
    model = "schema-only"

    def model_name(self):
        return "schema-only"

    def cfg(self):
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--project-dir", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--distribution", default="")
    args = parser.parse_args()
    audio = wsl_host_path(args.audio, args.distribution) if args.distribution else Path(args.audio)
    text_path = wsl_host_path(args.text, args.distribution) if args.distribution else Path(args.text)
    text = text_path.read_text(encoding="utf-8").strip()
    duration = mfa.canonical_audio_duration(audio)
    prompts = []

    def ask(session, content, validator=None, **kwargs):
        payload = json.loads(content)
        prompts.append(payload)
        response = []
        for item in payload["items"]:
            if "SEMANTIC BILINGUAL SUBTITLE TRANSLATION" in session.system_prompt:
                words = item["en"].split()
                middle = max(1, len(words) // 2)
                parts = [" ".join(words[:middle]), " ".join(words[middle:])]
                response.append({"id": item["id"], "zh": "Whole schema translation.", "split_events": [
                    {"en": part, "zh": f"schema fixture {index}."} for index, part in enumerate(parts, 1) if part]})
            elif "split_events" in item:
                parts = [{"id": event["id"], "zh": f"schema fixture {index}."}
                         for index, event in enumerate(item["split_events"], 1)]
                response.append({"id": item["id"], "zh": "".join(part["zh"] for part in parts), "split_events": parts})
            else:
                adopted = item["en"].lower()
                changed = adopted != item["en"]
                response.append({"id": item["id"], "en": adopted, "decision": "edit" if changed else "keep",
                                 "basis": "readability" if changed else "candidate", "reason": "Case fixture" if changed else ""})
        answer = json.dumps({"items": response})
        return answer, validator(answer)

    with tempfile.TemporaryDirectory(prefix="mfa smoke ") as directory:
        root = Path(directory)
        shutil.copyfile(audio, root / "sample.wav")
        source = root / "sample.json"
        source.write_text(json.dumps({"language": "en", "segments": [{"id": 1, "text": text,
                                 "start": 0, "end": duration, "words": []}]}), encoding="utf-8")
        template = Path(args.project_dir) / "core/subtitle_translation/examples/template.ass.example"
        arguments = [str(source), "--template", str(template)]
        settings = {"MFA_ENABLED": "1", "PIPELINE_SKIP_SCENE_DETECTION": "1", "PROOFREAD": "0", }
        def glossary(transcript, ctx, llm, options):
            Path(ctx.glossary).write_text("# Schema fixture glossary\n", encoding="utf-8")
            return ctx.glossary
        with patch.dict(os.environ, {"SUBTITLE_TRANSLATION_PROJECT_DIR": str(Path(args.project_dir).resolve())}), \
             patch.object(t, "load_env", return_value={"TRANSLATE_PROVIDER": "fixture"}), \
             patch.object(t, "translate_llm_from_env", return_value=FixtureLLM()), \
             patch.object(t, "glossary_llm_from_env", return_value=FixtureLLM()), \
             patch.object(t, "build_glossary", side_effect=glossary), \
             patch.object(t.ChatSession, "ask_validated", autospec=True, side_effect=ask), \
             patch.object(mfa, "run_command", wraps=mfa.run_command) as commands:
            if t.main(arguments, stage_overrides=settings):
                raise RuntimeError("first MFA CLI run failed")
            first_count = commands.call_count
            first_prompts = len(prompts)
            if t.main(arguments, stage_overrides=settings):
                raise RuntimeError("cached MFA CLI run failed")
            if commands.call_count - first_count != 1 or len(prompts) != first_prompts:
                raise RuntimeError(f"cache miss: second-run model commands={commands.call_count-first_count}, LLM calls={len(prompts)-first_prompts}")
        result = json.loads((root / "sample.beautified.json").read_text(encoding="utf-8"))
        if result["_asr"]["aligner"] != "mfa" or not all(segment["word_timing"]["status"] == "available" for segment in result["segments"]):
            raise RuntimeError("real alignment did not yield a complete source word axis")
        output = (root / "sample.en-zh.ass").read_text(encoding="utf-8")
        if json.loads(source.read_text(encoding="utf-8"))["segments"][0]["text"] != text:
            raise RuntimeError("ASR source was modified")
        print(json.dumps({"duration": duration, "words": sum(len(segment["words"]) for segment in result["segments"]),
                          "segments": len(result["segments"]), "events": output.count("Dialogue:"),
                          "cached_model_commands": 1, "second_run_llm_calls": 0,
                          "translation_quality_tested": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
