---
description: "Get weather information for any location"
tags: [weather, forecast, temperature, rain, sunny, cloudy, humidity, wind, climate]
---
# Weather Context

Use `get_weather(location)` to get weather information.

## Usage
```python
result = await get_weather(location="Minsk")
return await response(text=result)
```

```python
result = await get_weather(location="London", forecast=true)
return await response(text=result)
```

## Features
- Current weather conditions
- Temperature in Celsius
- Humidity and wind speed
- Optional 3-day forecast

## Location Format
- City name: "Minsk", "London", "New York"
- Works with any language input
