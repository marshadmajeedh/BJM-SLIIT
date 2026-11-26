"""Optional Groq adapter; calling it sends the supplied context to Groq."""

import os


def generate(prompt):
    from groq import Groq

    model = os.environ.get("GROQ_MODEL")
    if not model or not os.environ.get("GROQ_API_KEY"):
        raise ValueError("Set GROQ_MODEL and GROQ_API_KEY before using Groq")
    client = Groq(api_key=os.environ["GROQ_API_KEY"], timeout=60, max_retries=2)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "Generate regression tests as JSON."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    return response.choices[0].message.content
