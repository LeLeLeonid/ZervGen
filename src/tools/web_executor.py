import requests
from bs4 import BeautifulSoup
from ..protocol import TaskOutput

def execute(url: str) -> TaskOutput:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        clean_text = soup.get_text(separator=' ', strip=True)
        return TaskOutput(status="success", result=clean_text[:4000])
    except requests.RequestException as e:
        return TaskOutput(status="error", result=f"Failed to fetch URL: {e}")