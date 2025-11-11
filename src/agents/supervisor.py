import json
import logging
from ..tools.pollinations_client import PollinationsClient, ApiClientError

SYSTEM_PROMPT = """
SYSTEM_PROMPT: ZERVGEN_SUPERVISOR_0.1
ROLE: You are the Supervisor, the central orchestrator of the ZervGen Framework. Your expertise is deconstructing user goals into precise, machine-readable JSON execution plans.
CORE_DIRECTIVE: Receive a high-level user goal and convert it into a JSON array of sequential tool calls for Executor agents. You create the plan; you do not execute it.
OPERATIONAL_PROTOCOLS:
1. Deconstruct: Analyze the user's goal to understand the core intent and all required steps.
2. Plan: Create a logical sequence of actions.
3. Format: Your final output MUST be only a valid JSON array `[]` of step objects `{}`.
*** PARAMETER_PRIORITY (CRITICAL_PROTOCOL) ***
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
        """
        It accepts common key variations (like 'tool') and normalizes them to the
        standard 'tool_name' before validating the final structure.
        """
        if not isinstance(plan, list):
            raise ValueError("Plan must be a list.")

        normalized_plan = []
        for step in plan:
            if not isinstance(step, dict):
                raise ValueError(f"Each step in the plan must be a dictionary. Found: {type(step)}")
            
            if 'tool' in step and 'tool_name' not in step:
                step['tool_name'] = step.pop('tool')
            
            if "tool_name" not in step or "params" not in step:
                raise ValueError("Each step must contain 'tool_name' and 'params' keys.")
            
            if not isinstance(step["params"], dict):
                 raise ValueError("The 'params' value must be a dictionary.")

            normalized_plan.append(step)
            
        return normalized_plan

    def create_plan(self, user_goal: str) -> list[dict]:
        """Uses an LLM to generate, normalize, and validate a JSON plan."""
        full_prompt = f"{SYSTEM_PROMPT}\n\nUser Goal: \"{user_goal}\"\n\nYOUR REQUIRED JSON OUTPUT:"
        
        raw_plan = self.client.generate_text(prompt=full_prompt, model="openai")
        
        try:
            if "```json" in raw_plan:
                raw_plan = raw_plan.split("```json")[1].split("```")
            parsed_plan = json.loads(raw_plan)
            validated_plan = self._normalize_and_validate_plan(parsed_plan)
            return validated_plan

        except (json.JSONDecodeError, ValueError, IndexError) as e:
            logging.error(f"Failed to parse or validate plan. Raw LLM response was: {raw_plan}")
            raise ApiClientError(f"Supervisor could not create a valid plan. Please try rephrasing your request. (Internal error: {e})")