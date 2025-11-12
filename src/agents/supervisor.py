import json
import logging
from ..tools.pollinations_client import PollinationsClient, ApiClientError

SYSTEM_PROMPT = """
SYSTEM PROMPT: ZERVGEN_SUPERVISOR_0.2
ROLE: You are the Supervisor, the central orchestrator of the ZervGen Framework. Your expertise is deconstructing user goals into precise, machine-readable JSON execution plans.
CORE_DIRECTIVE: Receive a high-level user goal and convert it into a JSON array of sequential tool calls for Executor agents. You create the plan; you do not execute it.
PROTOCOLS:
1. Deconstruct: Analyze the user's goal to understand the core intent and all required steps.
2. Plan: Create a logical sequence of actions.
3. Format: Your final output MUST be only a valid JSON array `[]` of step objects `{}`.
*** PARAMETER PRIORITY (CRITICAL PROTOCOL) ***
User-specified technical parameters (like `width`, `height`, `model`) are absolute requirements. You MUST include them in the plan's `params` object exactly as the user states them.
TOOLS:
1.  `generate_image`
    - Parameters: `prompt` (string, required), `width` (int, optional), `height` (int, optional), `model` (str, optional), `enhance` (bool, optional).
2.  `generate_text`
    - Parameters: `prompt` (string, required), `model` (str, optional), `temperature` (float, optional).
"""

class Supervisor:
    """Agent 0: Deconstructs user goals into machine-readable JSON plans."""
    def __init__(self, client: PollinationsClient):
        self.client = client

    def _normalize_and_validate_plan(self, plan: list) -> list:
        if not isinstance(plan, list): raise ValueError("Plan must be a list.")
        normalized_plan = []
        for step in plan:
            if not isinstance(step, dict): raise ValueError(f"Step must be a dict. Found: {type(step)}")
            if 'tool' in step: step['tool_name'] = step.pop('tool')
            if "tool_name" not in step or "params" not in step: raise ValueError("Step must contain 'tool_name' and 'params'.")
            if not isinstance(step["params"], dict): raise ValueError("'params' must be a dict.")
            normalized_plan.append(step)
        return normalized_plan
    
    def create_pattern_creation_plan(self, meta_pattern_prompt: str, user_goal: str) -> list[dict]:
        """Creates the deterministic, two-step plan for generating and saving a new pattern."""
        final_pattern_engine_prompt = f"{meta_pattern_prompt}\n\n**[GOAL]:**\n`{user_goal}`"
        
        name_and_desc_prompt = f"Based on the user goal '{user_goal}', generate a short, snake_case `pattern_name` and a concise one-sentence `explanation` for it. Output only a valid JSON object with these two keys, and nothing else."
        
        try:
            name_and_desc_raw = self.client.generate_text(prompt=name_and_desc_prompt)
            name_and_desc = json.loads(name_and_desc_raw)
        except (json.JSONDecodeError, KeyError) as e:
            logging.error(f"Failed to generate name/description. Raw response: {name_and_desc_raw}")
            raise ApiClientError(f"Could not generate a valid name and explanation for the new pattern. Error: {e}")

        plan = [
            {
                "tool_name": "generate_text",
                "params": {
                    "prompt": final_pattern_engine_prompt
                }
            },
            {
                "tool_name": "generate_pattern",
                "params": {
                    "pattern_name": name_and_desc['pattern_name'],
                    "explanation": name_and_desc['explanation']
                }
            }
        ]
        return plan

    def create_plan_from_pattern(self, pattern_prompt: str, user_goal: str) -> list[dict]:
        """Uses a pattern to generate, normalize, and validate a JSON plan."""

        full_prompt = f"{pattern_prompt}\n\n{user_goal}"
        
        plan = [
            {
                "tool_name": "generate_text",
                "params": {
                    "prompt": full_prompt
                }
            }
        ]
        
        return plan