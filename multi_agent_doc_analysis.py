import os, json, asyncio
import chainlit as cl
from pathlib import Path
from docx import Document
from PyPDF2 import PdfReader
from datetime import datetime
from agent_framework import WorkflowBuilder, ChatAgent, ai_function, ChatMessage
from foundry_local import FoundryLocalManager

# ---- Setup ----
model_name = "phi-4"
manager = FoundryLocalManager()
model = manager.load_model(model_name)
service_uri = f"{manager.service_uri}/v1"
api_key = manager.api_key


from agent_framework.openai import OpenAIChatClient
chat_client = OpenAIChatClient(model_id=model.id, base_url=service_uri, api_key=api_key)

# ---- Utils ----
def get_text(file_path):
    if file_path.endswith(".pdf"):
        reader = PdfReader(open(file_path, "rb"))
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    elif file_path.endswith(".docx"):
        doc = Document(file_path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    raise ValueError("Only .docx or .pdf supported")

def clean_json(raw):
    import re
    raw = raw.strip()
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()

# ---- AI Tools ----
@ai_function
async def extract_sections(text: str):
    msg = f"Split text into sections as JSON. Return ONLY valid JSON and nothing else:\n{{'sections':[{{'title':'','content':''}}]}}\n{text[:8000]}"
    messages = [ChatMessage(role="user", text=msg)]
    resp = await chat_client.get_response(messages)
    raw = resp.messages[0].text
    print("RAW SECTIONS OUTPUT:", raw)
    try:
        raw_clean = clean_json(raw)
        return json.loads(raw_clean)
    except Exception as e:
        print("Section JSON error:", e)
        return {"sections": [{"title":"Full Document","content":text}]}


@ai_function
async def extract_entities(text: str):
    msg = (
        "Extract named entities as JSON. Return ONLY valid JSON and nothing else:\n"
        "{'people':[], 'organizations':[], 'locations':[]}\n"
        f"{text[:6000]}"
    )
    messages = [ChatMessage(role="user", text=msg)]
    resp = await chat_client.get_response(messages)
    raw = resp.messages[0].text
    print("RAW ENTITIES OUTPUT:", raw)
    try:
        raw_clean = clean_json(raw)
        return json.loads(raw_clean)
    except Exception as e:
        print("Entity JSON error:", e)
        return {"people": [], "organizations": [], "locations": []}


@ai_function
async def analyze(sections, entities):
    msg = (
        "Summarize and rate sentiment as JSON. Return ONLY valid JSON and nothing else.\n"
        '{"summary":"...","sentiment":"neutral"}\n'
        f"Sections: {json.dumps(sections)}\nEntities: {json.dumps(entities)}"
    )
    messages = [ChatMessage(role="user", text=msg)]
    resp = await chat_client.get_response(messages)
    raw = resp.messages[0].text
    print("RAW ANALYZE OUTPUT:", raw)
    try:
        raw_clean = clean_json(raw)
        return json.loads(raw_clean)
    except Exception as e:
        print("Analyze JSON error:", e)
        return {"summary": "Failed", "sentiment": "neutral"}

# ---- Workflow ----
def build_workflow():
    extractor = ChatAgent(chat_client, name="Extractor", tools=[extract_sections])
    ner = ChatAgent(chat_client, name="NER", tools=[extract_entities])
    analyzer = ChatAgent(chat_client, name="Analyzer", tools=[analyze])
    return (
        WorkflowBuilder()
        .add_agent(extractor, id="extractor")
        .add_agent(ner, id="ner")
        .add_agent(analyzer, id="analyzer", output_response=True)
        .add_edge(extractor, ner)
        .add_edge(ner, analyzer)
        .set_start_executor(extractor)
        .build()
    )

workflow = build_workflow()

# ---- Chainlit UI ----
@cl.on_message
async def main(msg):
    f = msg.elements[0] if msg.elements else None
    if not f:
        await cl.Message("Upload a .pdf or .docx file.").send()
        return

    # Step 1: Extract text
    await cl.Message("📄 Extracting text...").send()
    print("📄 Extracting text...")
    text = get_text(f.path)
    await cl.Message(f"✅ Text extracted ({len(text)} characters)").send()
    print(f"✅ Text extracted ({len(text)} characters)")

    # Step 2: Extract sections
    sections = await extract_sections(text)
    print("Sections parsed:", sections)
    await cl.Message(f"✅ {len(sections['sections'])} sections extracted").send()

    entities = await extract_entities(text)
    print("Entities parsed:", entities)
    found_entities = sum(len(v) for v in entities.values())
    await cl.Message(f"✅ {found_entities} entities found").send()

    analysis = await analyze(sections['sections'], entities)
    print("Analysis result:", analysis)
    await cl.Message("✅ Analysis complete").send()

    # Save and show results
    record = {
        "filename": f.name,
        "timestamp": datetime.utcnow().isoformat(),
        "model": model.id,
        "result": analysis,
        "entities": entities,
        "sections": sections,
    }
    out_json = f"analysis_{f.name}.json"
    with open(out_json, "w") as x:
        json.dump(record, x, indent=2)
    await cl.Message(
        f"# Summary\n{analysis.get('summary','')}\n\n### Sentiment: {analysis.get('sentiment','')}",
        elements=[cl.File(name=out_json, path=out_json)]
    ).send()
    print(f"# Summary\n{analysis.get('summary','')}\n### Sentiment: {analysis.get('sentiment','')}")


# ---- CLI ----
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", help="Path to file (.pdf/.docx)")
    args = parser.parse_args()
    if not args.file: print("Usage: --file <path>"); exit()
    text = get_text(args.file)
    wf = build_workflow()
    out = asyncio.run(wf.run(text))
    print(json.dumps(out, indent=2))