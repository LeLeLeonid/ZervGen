import json
from .toolbox import Toolbox
from ..protocol import TaskInput
from ..tools.pollinations_client import PollinationsClient

class Orchestrator:
    def __init__(self, toolbox: Toolbox, llm_client: PollinationsClient):
        self.toolbox = toolbox
        self.llm_client = llm_client
        self.system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        return f"""# IDENTITY
You are the Orchestrator, a hyper-logical planner. Your sole function is to deconstruct a user's [GOAL] into a precise, machine-readable JSON plan.

# AVAILABLE TOOLS & PARAMETERS
{self.toolbox.get_manifest_for_prompt()}

# CRITICAL PROTOCOLS
1.  Analyze the [GOAL] and generate a JSON array of tasks.
2.  For each task, select the SINGLE most appropriate `task_name`.
3.  Parameterize THOROUGHLY using the exact parameter names listed for each tool. If the user asks for a 'FullHD photo', you MUST use `"width": 1920, "height": 1080`.
4.  Use `{{{{PREVIOUS_RESULT}}}}` as a placeholder for the output of a previous step.
5.  Your output MUST be a single, valid JSON array of task objects.

# GOAL
**[GOAL]:** "{{GOAL}}"
"""

    def _create_plan(self, goal: str) -> list[TaskInput]:
        prompt = self.system_prompt.format(GOAL=goal)
        response_text = self.llm_client.generate_text(prompt=prompt)
        if '```json' in response_text:
            response_text = response_text.split('```json')[1].split('```')
        
        plan_data = json.loads(response_text)
        
        validated_plan = []
        for step in plan_data:
            task_name = step.get('task_name')
            params = step.get('params', step.get('parameters'))

            if params is None and task_name is not None:
                params = {key: value for key, value in step.items() if key != 'task_name'}

            if task_name is None or params is None:
                raise KeyError(f"A step in the generated plan is invalid. Step: {step}")
            
            validated_plan.append(TaskInput(task_name=task_name, params=params))
        return validated_plan

    def execute_goal(self, goal: str):
        plan = self._create_plan(goal)
        print("\n[Orchestrator] Plan:")
        for task in plan: print(f"  - {task.task_name}: {task.params}")

        previous_result = None
        for task in plan:
            print(f"\n--- Executing: {task.task_name} ---")
            tool_function = self.toolbox.get_tool(task.task_name)
            if not tool_function:
                print(f"  [ERROR] Tool '{task.task_name}' not found. Halting.")
                break
            
            current_params = task.params.copy()
            if isinstance(current_params, dict):
                for key, value in current_params.items():
                    if isinstance(value, str) and "{{PREVIOUS_RESULT}}" in value:
                        context = previous_result
                        if isinstance(previous_result, dict):
                            context = previous_result.get('clean_text', json.dumps(previous_result))
                        
                        instructions = current_params.get('prompt', '')
                        final_prompt = f"{instructions}\n\nData to process:\n---\n{context}\n---"
                        current_params = {'prompt': final_prompt}
                        break
            
            output = tool_function(**current_params)
            print(f"  Status: {output.status}")
            print(f"  Result: {output.result}")
            
            if output.status == "success":
                previous_result = output.result
            else:
                print(f"  [ERROR] Task failed. Halting.")
                break
        
        print("\n--- Goal complete. ---")