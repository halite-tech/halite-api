import requests

import os

def prompt_tools(problems: list[str]) -> list[str]:
    prompt = f"""
    You are an experienced restaurant owner with 10+ years of operational experience. Your goal is to identify the best AI tools to solve each of the restaurant's problems listed below.

    For **EACH problem**, find exactly **4 AI tools**, divided into two categories:

    **Category 1: Platform-integrated solutions (2 tools)**
    - Find 2 tools that can be directly downloaded, installed, or integrated into the platforms and software I already use.
    - Prioritize ease of implementation, compatibility, and minimal disruption to existing workflows.

    **Category 2: Highest business ROI (2 tools)**
    - Find 2 tools that offer the greatest potential to reduce operating costs, increase revenue, or improve profitability.
    - Prioritize measurable ROI, reliability, proven results, and ease of employee adoption.

    **For every tool, provide:**
    1. **Tool name and homepage:** Link directly to its official website.
    2. **Pricing:** Provide pricing plans or estimated costs. Clearly distinguish publicly listed prices from custom/quote-based pricing.
    3. **Core functionality:** Explain exactly how the tool addresses the specific problem.
    4. **Platform compatibility:** Identify which existing restaurant platforms it integrates with and how the integration works.
    5. **ROI:** Explain the expected financial or operational benefits. Include quantified results where supported by credible evidence.
    6. **Reliability:** Evaluate its track record, customer reviews, and evidence of effectiveness.
    7. **Adoptability:** Assess implementation difficulty, employee training requirements, and how easily it fits into restaurant operations.

    **Research requirements:**
    - Conduct thorough research. Accuracy and reliability are more important than speed.
    - Prioritize established products with real restaurant customers and proven use cases.
    - Verify that each product is currently available and that its integrations and pricing are accurate.
    - Use official product websites, documentation, pricing pages, customer case studies, and credible third-party reviews.
    - Do not recommend tools simply because they advertise AI capabilities. They must meaningfully solve the stated problem.
    - Avoid recommending the same tool repeatedly unless it genuinely provides the best solution for multiple problems.
    - Do not fabricate pricing, integrations, ROI figures, or customer results. Explicitly label information that cannot be verified.
    - If fewer than 4 credible solutions exist for a problem, explain this rather than padding the results with weak recommendations.

    **Final output:**
    Organize the research by problem, with exactly 4 recommendations per problem whenever credible options exist. Clearly distinguish the 2 platform-integrated tools from the 2 highest-ROI alternatives.

    End each problem's section with a brief comparison identifying which tool you would actually choose as a restaurant owner and why.

    **STRICT OUTPUT FORMAT REQUIREMENT**

    The entire response MUST follow the exact hierarchical structure below. Do not use tables. Do not group tools into separate sections outside their respective problems. Every problem must contain its own 4 tools.

    Problem 1: [Problem name]

    - Tool 1: [Tool name]
    - Category: Platform-integrated / Highest ROI
    - Homepage:
    - Pricing:
    - Core functionality:
    - How it solves the problem:
    - Platform compatibility:
    - Expected ROI:
    - Reliability:
    - Adoptability:

    - Tool 2: [Same structure]
    - Tool 3: [Same structure]
    - Tool 4: [Same structure]

    - Final recommendation: [Which tool you would choose and why.]

    Problem 2: [Problem name]

    - Tool 1:
    [Repeat the exact same structure.]

    - Tool 2:
    - Tool 3:
    - Tool 4:

    - Final recommendation:

    Continue this exact structure for every problem provided.

    **Formatting rules:**
    1. Every problem must have its own heading.
    2. Every problem must contain exactly 4 tools whenever 4 credible solutions exist.
    3. Each tool must contain all 9 information fields listed above.
    4. Never combine multiple problems under one heading.
    5. Never move tools into a separate consolidated list or comparison table.
    6. Maintain identical formatting and field order throughout the entire response.
    7. Do not omit fields; use "Not publicly available" when information cannot be verified.
    8. Keep all recommendations directly under the problem they address.

    **Problems to research:**

    {problems}
    """
    return prompt

def get_tools(problems: list[str]) -> list[str]:
    print("getting tools")
    prompt = prompt_tools(problems)
    print("prompt:")
    key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Add it to AISB/.env and restart uvicorn "
            "(python-dotenv loads .env in backend.api on startup)."
        )
    response = requests.post(
            "https://openrouter.ai/api/v1/responses",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-4o-mini-2024-07-18",
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
    # if "error" in data:
    #     raise RuntimeError(f"{data['error']['code']}: {data['error']['message']}")

    message = next(item for item in reversed(data["output"]) if item["type"] == "message")
    result = next(part["text"] for part in message["content"] if part["type"] == "output_text")
    print(result)  # JSON string matching your schema
    return result
    # except Exception as e:
    #     print("error: ", e)
    #     return None


