
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
ce = boto3.client("ce", region_name="us-east-1")
sns = boto3.client("sns")

TABLE_NAME = os.environ["CONFIG_TABLE"]
TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
DEFAULT_PROJECT = os.environ["DEFAULT_PROJECT_ID"]

table = dynamodb.Table(TABLE_NAME)


def get_config(project_id):
    response = table.get_item(
        Key={"project_id": project_id},
        ConsistentRead=True
    )

    item = response.get("Item")
    if not item:
        raise ValueError(f"Project not found: {project_id}")

    config = item["config"]

    # Also support a JSON string stored in DynamoDB
    if isinstance(config, str):
        config = json.loads(config)

    if not config.get("enabled", False):
        raise ValueError("Project monitoring is disabled")

    if config.get("currency", "USD") != "USD":
        raise ValueError("This POC supports USD only")

    if config.get("period", "MONTHLY") != "MONTHLY":
        raise ValueError("This POC supports monthly limits")

    return config


def get_service_costs(config):
    today = datetime.now(timezone.utc).date()

    start = today.replace(day=1).isoformat()
    end = (today + timedelta(days=1)).isoformat()

    paginator = ce.get_paginator("get_cost_and_usage")

    pages = paginator.paginate(
        TimePeriod={
            "Start": start,
            "End": end
        },
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={
            "Tags": {
                "Key": config["tag_key"],
                "Values": [config["tag_value"]]
            }
        },
        GroupBy=[
            {
                "Type": "DIMENSION",
                "Key": "SERVICE"
            }
        ]
    )

    costs = {}

    for page in pages:
        for period in page["ResultsByTime"]:
            for group in period["Groups"]:
                service = group["Keys"][0]

                metric = group["Metrics"]["UnblendedCost"]
                if metric["Unit"] != "USD":
                    raise ValueError("Unexpected cost currency")

                amount = Decimal(metric["Amount"])

                costs[service] = (
                    costs.get(service, Decimal("0"))
                    + amount
                )

    return costs


def claim_alert(project_id, alert_key):
    """
    Atomically claim an alert in DynamoDB.
    Prevents concurrent Lambda executions from
    publishing the same threshold alert.
    """
    try:
        table.update_item(
            Key={"project_id": project_id},
            UpdateExpression=(
                "SET alert_state.#alert_key = :claimed"
            ),
            ConditionExpression=(
                "attribute_exists(project_id) AND "
                "attribute_not_exists("
                "alert_state.#alert_key)"
            ),
            ExpressionAttributeNames={
                "#alert_key": alert_key
            },
            ExpressionAttributeValues={
                ":claimed": "CLAIMED"
            }
        )
        return True

    except ClientError as error:
        if (
            error.response["Error"]["Code"]
            == "ConditionalCheckFailedException"
        ):
            return False
        raise


def publish_alert(project_id, alerts):
    lines = [
        "AWS PROJECT COST ALERT",
        f"Project: {project_id}",
        "",
        "Monthly service threshold breaches:",
        ""
    ]

    for alert in alerts:
        lines.extend([
            f"Service: {alert['service']}",
            f"Current cost: ${alert['cost']}",
            f"Threshold: ${alert['threshold']}",
            f"Usage: {alert['percentage']}%",
            ""
        ])

    lines.append(
        "Review the AWS Billing and Cost "
        "Management console."
    )

    response = sns.publish(
        TopicArn=TOPIC_ARN,
        Subject=f"AWS Cost Alert - {project_id}",
        Message="\n".join(lines)
    )

    return response["MessageId"]


def lambda_handler(event, context):
    event = event or {}
    project_id = event.get(
        "project_id", DEFAULT_PROJECT
    )

    config = get_config(project_id)
    costs = get_service_costs(config)

    month = datetime.now(
        timezone.utc
    ).strftime("%Y-%m")

    alerts = []

    for service, threshold_value in (
        config["thresholds"].items()
    ):
        threshold = Decimal(str(threshold_value))

        if threshold <= 0:
            raise ValueError(
                f"Invalid threshold for {service}"
            )

        cost = costs.get(service, Decimal("0"))

        if cost < threshold:
            continue

        alert_key = (
            f"{month}#{service}#{threshold}"
        )

        if not claim_alert(project_id, alert_key):
            logger.info(
                "Alert already claimed: %s", alert_key
            )
            continue

        alerts.append({
            "service": service,
            "cost": str(round(cost, 2)),
            "threshold": str(threshold),
            "percentage": str(
                round(cost / threshold * 100, 1)
            )
        })

    message_id = None

    if alerts:
        message_id = publish_alert(
            project_id, alerts
        )

    logger.info(
        "Project=%s, services=%s, alerts=%s",
        project_id,
        len(costs),
        len(alerts)
    )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "project_id": project_id,
            "period": month,
            "service_costs": {
                name: str(round(value, 2))
                for name, value in costs.items()
            },
            "new_alerts": alerts,
            "sns_message_id": message_id
        })
    }
