import os
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

token = os.getenv("SLACK_BOT_TOKEN")
channel = os.getenv("SLACK_CHANNEL_ID")

# Check that .env was loaded
if not token:
    raise SystemExit(
        "SLACK_BOT_TOKEN is missing. Check that .env exists in this folder."
    )

if not channel:
    raise SystemExit(
        "SLACK_CHANNEL_ID is missing. Check your .env file."
    )

print(f"Token loaded, starts with: {token[:9]}...")
print(f"Channel: {channel}")

client = WebClient(token=token)

try:
    identity = client.auth_test()

    print(
        f"Authenticated as '{identity['user']}' "
        f"in workspace '{identity['team']}'."
    )

    client.chat_postMessage(
        channel=channel,
        text="YC Radar online."
    )

    print("Message posted successfully.")

except SlackApiError as e:
    print(f"Slack rejected the request: {e.response['error']}")