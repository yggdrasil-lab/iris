# Iris

> I am Iris, Herald of Olympus and Messenger of the Gods in the Yggdrasil ecosystem. My domain is Notifications, Messaging, and the Flow of Information. I carry the master's words across the digital divide.

## Mission

I am the central switchboard and notification gateway of your infrastructure. My mission is to ensure no critical alert remains unheard, translating internal signals into Email or Chat and relaying SMTP traffic.

## Core Philosophy

*   **Unified Input**: I am the single entry point for all notifications.
*   **Smart Routing**: I choose the path that fits the priority—Email for logs, Chat for attention.

---

## Tech Stack

*   **Python 3.11+**: Core logic for the notification API.
*   **FastAPI**: Gateway REST API.
*   **Boto3**: AWS SES integration.

## Architecture

The system operates as a single microservice:

### Notification Gateway (API & SMTP)
*   **HTTP Interface**: RESTful API for service integration.
*   **SMTP Gateway**: Listener on port 2525 for legacy mail routing.
*   **AWS SES**: Backend for reliable email delivery.

## Prerequisites

- **AWS Account**: Verified Sender Identity in Amazon SES.
- **Docker & Docker Compose**
- **aether-net**: External Docker network (see `Forge/yggdrasil-os`).

## Directory Structure

```text
iris/
├── app/              # Notification Gateway (API & SMTP)
│   ├── core/
│   ├── services/
│   ├── main.py
│   ├── Dockerfile
│   └── requirements.txt
└── docker-compose.yml
```

## Setup Instructions

### 1. Repository Initialization

```bash
git clone git@github.com:yggdrasil-lab/iris.git iris
cd iris
```

### 2. Configuration

Update `.env` (or Github Secrets for Prod) with:

- `AWS_ACCESS_KEY_ID`: IAM Access Key.
- `AWS_SECRET_ACCESS_KEY`: IAM Secret Key.
- `AWS_REGION`: AWS Region (e.g., `ap-southeast-2`).
- `EMAIL_SENDER`: Your verified sender address.

## Execution

### Development (Local)
For local development, you can use `docker compose up --build`. Ensure your `.env` file is populated.

### Production Deployment
Production deployment is handled automatically via the GitHub Actions workflow defined in `.github/workflows/deploy.yml`.

## Usage

### 1. HTTP API
**Endpoint:** `POST /notify`
```bash
curl -X POST http://localhost:8000/notify \
  -H "Content-Type: application/json" \
  -d '{
    "channel": "email",
    "recipient": "user@example.com",
    "subject": "Hello",
    "body": "World"
  }'
```

### 2. SMTP Gateway
Connect legacy services to port **2525**. Incoming mail will be relayed via the configured SES identity.
