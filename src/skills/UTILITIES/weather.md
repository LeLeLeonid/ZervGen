---
name: weather
description: "Get weather information for any location"
when_not: "the request does not name a place or date"
tags: [weather, forecast, temperature, rain, sunny, cloudy, humidity, wind, climate]
---
# Weather Context

Use `weather(location)` to get weather information.

## Usage
```python
result = await weather(location="Minsk")
return await response(text=result)
```

```python
result = await weather(location="London", forecast=True)
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
