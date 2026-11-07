import requests

import os

def prompt_tools(problems: list[str]) -> list[str]:
    prompt = f"""
    You are a restaurant owner that has over 10 years of experience. your goal is to find the
    best ai tools for the following problems, with 2 that can be downloaded straight into 
    my platforms that I use and 2 that best decrease cost/increase profit with the 
    biggest ROI and reliability and best adoptability you can find. You can take your time
    to find these solutions as I need them to be reliable. Here are the problems that I need
    4 AI tools for and their pricing + home pages for:
    {problems}
    """
    return prompt

def get_tools(problems: list[str]) -> list[str]:
    prompt = prompt_tools(problems)
    response = requests.post(
        "https://openrouter.ai/api/v1/responses",
        headers={
            "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
            "Content-Type": "application/json",
        },
        json={
            "model": "openai/gpt-5.2",
            "input": prompt,
            "tools": [
                {
                    "type": "openrouter:web_fetch",
                    "parameters": {
                        "max_uses": 4,
                    },
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "tool_rec",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "tool_number": {"type": "integer"},
                            "tool_name": {"type": "string"},
                            "tool_description": {"type": "string"},
                            "tool_pricing": {"type": "string"},
                            "tool_home_page": {"type": "string"},
                        },
                        "required": ["tool_number", "tool_name", "tool_description", "tool_pricing", "tool_home_page"],
                        "additionalProperties": False,
                    },
                }
            },
        },
    )

    data = response.json()
    message = next(item for item in reversed(data["output"]) if item["type"] == "message")
    result = next(part["text"] for part in message["content"] if part["type"] == "output_text")
    print(result)  # JSON string matching your schema
    return result
