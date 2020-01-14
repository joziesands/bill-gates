import datetime
import json
import secrets
from os import path
import os
import urllib.parse

import boto3
from elasticsearch import NotFoundError

from databases import ES_DB

UI_HOST = os.environ["UI_HOST"]

parsed_ui_host = urllib.parse.urlparse(UI_HOST)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",  # TODO uncomment before launch # f"{parsed_ui_host.scheme or 'https'}://{parsed_ui_host.netloc}",
    "Access-Control-Allow-Credentials": True,
}


def get_api_url(event):
    request_context = event["requestContext"]
    return f"https://{request_context['domainName']}/{request_context['stage']}"


def get_submissions(event, context):
    fields_to_output = ["firstName", "debt", "story", "verifiedDate"]

    query_params = event.get("queryStringParameters") or {}

    # Comma separated list of specific submission ids to include
    # Max length 3
    include = query_params.get("include")
    if include:
        include = include.split(",")[:3]

    try:
        limit = int(query_params.get("limit"))
        assert 0 < limit < 200
    except (ValueError, AssertionError, TypeError):
        limit = 90

    try:
        from_ = int(query_params.get("from"))
        assert 0 < from_ < 9600
    except (ValueError, AssertionError, TypeError):
        from_ = 0

    results = ES_DB.search(
        index="submissions",
        body={
            "query": {"exists": {"field": "verifiedDate"}},
            "_source": fields_to_output,
            "sort": [{"verifiedDate": {"order": "desc"}}],
            "aggs": {"total_debt": {"sum": {"field": "debt"}}},
            "from": from_,
            "size": limit,
        },
    )

    total_debt = results["aggregations"]["total_debt"]["value"]

    submissions = [submission["_source"] for submission in results["hits"]["hits"]]

    if include:
        existing_submission_ids = {
            submission["_id"] for submission in results["hits"]["hits"]
        }
        for submission_id in include:
            if submission_id not in existing_submission_ids:
                try:
                    submission = ES_DB.get(index="submissions", id=submission_id)
                except NotFoundError:
                    continue

                existing_submission_ids.add(submission_id)

                # add submission to what is returned
                submissions.insert(
                    0,
                    {
                        k: v
                        for k, v in submission["_source"].items()
                        if k in fields_to_output
                    },
                )

    # TODO set something in the cookie perhaps to indicate when they got to page

    return {
        "statusCode": 200,
        "headers": {**CORS_HEADERS},
        "body": json.dumps(
            [
                {
                    "submissions": submissions,
                    "count_submissions": results["hits"]["total"]["value"],
                    "total_debt": total_debt,
                }
            ]
        ),
    }


def clean_email(email):
    parts = email.lower().split("@")
    return parts[0].replace(".", "").split("+")[0] + "@" + parts[1]


def create_submission_record(submission):
    return {
        "name": submission["name"],
        "firstName": submission["name"].split(" ")[0],
        "debt": submission["debt"],
        "story": submission["story"],
        "email": submission["email"],
        "emailClean": clean_email(submission["email"]),
        "createdDate": datetime.datetime.now().isoformat(),
        "tokenVerify": secrets.token_urlsafe(16).replace("-", ""),
        "tokenDelete": secrets.token_urlsafe(16).replace("-", ""),
    }


def mark_verified(submission):
    return {
        **submission,
        "tokenVerify": None,
        "verifiedDate": datetime.datetime.now().isoformat(),
    }


def send_email(submission_record, submission_id, api_url):
    client = boto3.client("ses")

    verify_url = (
        path.join(api_url, f"submissions/{submission_id}/verify")
        + f"?token={submission_record['tokenVerify']}"
    )
    delete_url = (
        path.join(api_url, f"submissions/{submission_id}/delete")
        + f"?token={submission_record['tokenDelete']}"
    )
    email_body = (
        f"<body>"
        f"<p>Thank you for providing your student debt story to Two Cent Stories! For security purposes, we'd like to verify your email before publishing your story on our website.</p>"
        f"<p>Please verify your email to publish your story.</p>"
        f"<br/>"
        f"<br/>"
        f"<a target='_blank' href='{verify_url}'>Verify Your Story</a>"
        f"<br/>"
        f"<br/>"
        f"<a target='_blank' href='{delete_url}'>Delete Your Story</a>"
        f"</body>"
    )

    print(email_body)
    response = client.send_email(
        Source="noreply@twocentstories.com",  # TODO make this not no-reply?
        Destination={"ToAddresses": [submission_record["email"],]},
        Message={
            "Subject": {"Data": "Confirm Your Story"},
            "Body": {"Html": {"Data": email_body}},
        },
        ReturnPath="complaints@twocentstories.com",
        # SourceArn='string',
        # ReturnPathArn='string',
        # ConfigurationSetName='string'
    )

    print(response)


def post_submission(event, context):
    print("event")
    print(event)
    print("context")
    print(context)
    # TODO check something in the cookie to help defeat trolls

    submission = json.loads(event["body"])
    record = create_submission_record(submission)

    # check that there isn't already a story from this email address
    existing_stories = ES_DB.count(
        index="submissions",
        body={"query": {"match": {"emailClean.keyword": record["emailClean"]}}},
    )

    if existing_stories["count"]:
        return {
            "statusCode": 409,
            "headers": {**CORS_HEADERS},
            "body": "Your story has already been submitted. Please check your email :)",
        }

    response = ES_DB.index(index="submissions", body=record)

    send_email(record, response["_id"], get_api_url(event))

    return {
        "statusCode": 200,
        "headers": {**CORS_HEADERS},
        "body": json.dumps({"id": response["_id"]}),
    }


def post_verified_submission(event, context):
    submission = json.loads(event["body"])
    record = create_submission_record(submission)
    verified_record = mark_verified(record)
    response = ES_DB.index(index="submissions", body=verified_record)

    return {
        "statusCode": 200,
        "headers": {**CORS_HEADERS},
        "body": json.dumps({"id": response["_id"]}),
    }


def verify_submission(event, context):
    submission_id = event["pathParameters"]["submissionId"]
    if not submission_id:
        return {
            "statusCode": 400,
            "headers": {**CORS_HEADERS},
            "body": "Submission id missing",
        }
    token = event.get("queryStringParameters", {}).get("token")
    if not token:
        return {"statusCode": 400, "headers": {**CORS_HEADERS}, "body": "Token missing"}

    try:
        submission = ES_DB.get(index="submissions", id=submission_id)
    except NotFoundError:
        return {
            "statusCode": 404,
            "headers": {**CORS_HEADERS},
            "body": "Story not found",
        }

    verify_token = submission["_source"]["tokenVerify"]

    redirect_url = f"{UI_HOST}?verified={submission_id}"

    if not verify_token:
        return {
            "statusCode": 302,
            "headers": {**CORS_HEADERS, "Location": redirect_url},
            "body": "Your story has already been verified! Thank you :)",
        }

    if token != verify_token:
        return {
            "statusCode": 403,
            "headers": {**CORS_HEADERS},
            "body": f"Token: {token} did not match",
        }

    # mark story is verified
    ES_DB.update(
        index="submissions",
        id=submission_id,
        body={
            "doc": {
                "tokenVerify": None,
                "verifiedDate": datetime.datetime.now().isoformat(),
            }
        },
        refresh="wait_for",
    )

    return {
        "statusCode": 302,
        "headers": {**CORS_HEADERS, "Location": redirect_url},
        "body": "Your story has been verified! Thank you :)",
    }


def delete_submission(event, context):
    submission_id = event["pathParameters"]["submissionId"]
    if not submission_id:
        return {
            "statusCode": 400,
            "headers": {**CORS_HEADERS},
            "body": "Submission id missing",
        }
    token = event.get("queryStringParameters", {}).get("token")
    if not token:
        return {"statusCode": 400, "headers": {**CORS_HEADERS}, "body": "Token missing"}

    try:
        submission = ES_DB.get(index="submissions", id=submission_id)
    except NotFoundError:
        return {
            "statusCode": 404,
            "headers": {**CORS_HEADERS},
            "body": "Story not found. Perhaps it has already been deleted!",
        }

    delete_token = submission["_source"]["tokenDelete"]

    if not delete_token:
        return {
            "statusCode": 200,
            "headers": {**CORS_HEADERS},
            "body": "Your story has already been deleted! Thank you :)",
        }

    if token != delete_token:
        return {
            "statusCode": 403,
            "headers": {**CORS_HEADERS},
            "body": f"Token: {token} did not match",
        }

    ES_DB.delete(index="submissions", id=submission_id, refresh="wait_for")

    return {
        "statusCode": 200,
        "headers": {**CORS_HEADERS},
        "body": "Your story has been deleted!",
    }
