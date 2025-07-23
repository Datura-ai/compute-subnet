# Renaming a Pod via Celium API

This guide walks you through how to **rename a pod using the Celium API**. Pod renaming is a simple, direct operation that updates the `pod_name` field of a pod identified by its UUID.

> **Goal**: To rename an existing pod using a single API call via `curl`, Postman, or code.

---

## Table of Contents
  - [Prerequisites](#prerequisites)
  - [Authentication](#authentication)
  - [Endpoint](#endpoint)
  - [Request Payload](#request-payload)
  - [Example Request (cURL)](#example-request-curl)
  - [Example Request (Python)](#example-request-python)
  - [Example Request (Node.js)](#example-request-nodejs)
  - [Successful Response](#successful-response)
    - [200: OK](#200-ok)
  - [Troubleshooting API Errors](#troubleshooting-api-errors)
    - [401: Unauthorized (Missing API Key)](#401-unauthorized-missing-api-key)
    - [401: Unauthorized (Invalid API Key)](#401-unauthorized-invalid-api-key)
    - [404: Pod Not Found](#404-pod-not-found)
    - [422: Unprocessable Entity](#422-unprocessable-entity)
      - [Invalid Data Type](#invalid-data-type)
      - [Malformed JSON](#malformed-json)
      - [Missing Body](#missing-body)
  - [Final Notes](#final-notes)
  - [Need Help?](#need-help)

---

## Prerequisites

- A valid Celium **API Key**. To create a new Celium API key, visit your Celium Dashboard
- The **pod ID** (UUID) of the pod you want to rename. You can get the pod ID from your Celium dashboard or through the [Celium GET pods API](https://celiumcompute.ai/documents/#/Support%20API%20Key%20Authentication/get_pods_pods_get)
- `curl`, Postman, or any HTTP client like Python `requests` or Node.js `fetch` to send the request

---

## Authentication

Celium requires the API key to be passed in the header:

```http
x-api-key: your_api_key_here
```

> **Note**: API keys currently have no expiry or rate limits. 

---

## Endpoint

To rename a pod:

```http
PATCH https://celiumcompute.ai/api/pods/{pod_id}
```

Replace `{pod_id}` with the actual UUID of the pod.

---

## Request Payload

Expected payload:

```json
{
  "pod_name": "new_pod_name"
}
```

> **Valid Examples**:
> - `{"pod_name": ""}` — Sets the name to an empty string  
> - `{"pod_name": "Hello world1!"}` — Allowed characters: letters, numbers, spaces, lower and upper-case characters, and special characters

> **Note**: Pod names are **not unique**. Multiple pods can have the same `pod_name`. Always use the `pod_id` as pod identifier.

---

## Example Request (cURL)

Here's how to send a PATCH request for renaming a pod with `curl`:

```bash
curl -X PATCH https://celiumcompute.ai/api/pods/{pod_id}   -H "x-api-key: your_api_key_here" -H "Content-Type: application/json" -d '{"pod_name": "new_pod_name"}'
```

---

## Example Request (Python)

Here's how to send a PATCH request for renaming a pod with the `requests` library in Python: 

```python
import requests

url = "https://celiumcompute.ai/api/pods/{pod_id}"
headers = {
    "x-api-key": "your_api_key_here",
    "Content-Type": "application/json"
}
data = {
    "pod_name": "new_pod_name"
}

response = requests.patch(url, headers=headers, json=data)
```

---

## Example Request (Node.js)

Here's how to send a PATCH request for renaming a pod with the `fetch` API in Node.js:

```javascript
const url = "https://celiumcompute.ai/api/pods/{pod_id}";

const headers = {
  "x-api-key": "your_api_key_here",
  "Content-Type": "application/json"
};

const data = JSON.stringify({
  pod_name: "new_pod_name"
});

fetch(url, {
  method: "PATCH",
  headers: headers,
  body: data
})
  .then(response => response.json())
  .catch(error => console.error("Error:", error));
```

---

## Successful Response

### 200: OK

#### Response Body:
```json
{
  "container_name": "container_6253d803-cb6c-4110-b2d0-6d265846edbd",
  "pod_name": "new_pod_name",
  "status": "RUNNING",
  "created_at": "2025-07-12T10:32:05.022741",
  "updated_at": "2025-07-12T10:39:05.693031",
  ... (truncated)
}
```

> A 200 OK response indicates that the rename was successful.

---

## Troubleshooting API Errors

### 401: Unauthorized (Missing API Key)

#### Response Body:
```json
{
  "detail": "API Key missing from headers"
}
```
**Fix**: Ensure the `x-api-key` header is included in the request.

---

### 401: Unauthorized (Invalid API Key)
#### Response Body:
```json
{
  "detail": "API key not found"
}
```
**Fix**: Ensure your API key is valid and correctly spelled.

---

### 404: Pod Not Found
#### Response Body:
```json
{
  "detail": "404: Pod not found"
}
```
**Fix**: Confirm the `pod_id` is correct.

---

### 422: Unprocessable Entity

#### Invalid Data Type
##### Response Body:
```json
{
  "detail": [
    {
      "type": "string_type",
      "loc": ["body", "pod_name"],
      "msg": "Input should be a valid string",
      "input": 123
    }
  ]
}
```
**Fix**: Ensure `pod_name` is a string.

#### Malformed JSON
##### Response Body:
```json
{
  "detail": [
    {
      "type": "json_invalid",
      "loc": ["body", 14],
      "msg": "JSON decode error",
      "input": {},
      "ctx": {
        "error": "Expecting value"
      }
    }
  ]
}
```
**Fix**: Check for missing keys or broken syntax in your JSON body.

#### Missing Body
##### Response Body:
```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["body"],
      "msg": "Field required",
      "input": null
    }
  ]
}
```
**Fix**: Ensure a JSON body is included with your request.

> **WARNING**: If you pass an incorrect key like `{"pd_name": "new_pod_name"}`, the API does not throw an error but also does **not** rename the pod. Your pod will still have the old name.

---

## Final Notes

- Use `pod_id` as the **only reliable identifier** for pod operations  
- Pod names are display-only and non-unique  
- Any string (even empty) is accepted as a pod name  
- There is currently no validation for duplicate or empty names

---

## Need Help?

For issues or unexpected behavior, check:
- Your API key permissions
- Correctness of `pod_id`
- JSON formatting

> _For further support, please [contact the Celium team](https://celiumcompute.ai/docs/intro#contact)._
