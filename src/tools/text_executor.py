from ..protocol import TaskOutput
from .pollinations_client import PollinationsClient

def execute(client: PollinationsClient, **params) -> TaskOutput:
    try:
        instructions = params.get('prompt') or params.get('input')
        if not instructions:
            raise ValueError("Requires a 'prompt' or 'input' parameter.")
        
        data = params.get('data_to_process')
        if data:
            final_prompt = f"{instructions}\n\nData to process:\n---\n{data}\n---"
        else:
            final_prompt = instructions

        final_params = params.copy()
        final_params['prompt'] = final_prompt
        if 'input' in final_params: del final_params['input']
        if 'data_to_process' in final_params: del final_params['data_to_process']

        result_text = client.generate_text(**final_params)
        return TaskOutput(status="success", result=result_text)
    except Exception as e:
        return TaskOutput(status="error", result=str(e))