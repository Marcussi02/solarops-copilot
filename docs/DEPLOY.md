# Deploying SolarOps Copilot to AWS

About 15 minutes, all in web consoles. After this, every push to `main` that passes CI deploys itself.

What you end up with: 6 Lambda functions, an SQS queue with a dead-letter queue, an S3 archive, an HTTP API, alarms, and a budget alert. There are **no AWS access keys anywhere**, because GitHub Actions signs in with short-lived OIDC credentials.

## 0. Accounts

1. **AWS.** Sign up at aws.amazon.com. It needs a card and phone verification; pick the free Basic support plan. Then:
   - Turn on **MFA for the root user**: account menu → *Security credentials* → *Assign MFA device*.
   - Choose **Asia Pacific (Sydney) `ap-southeast-2`** in the region picker (top right). Everything below goes in Sydney, next to AEMO's data.
2. **Supabase (free Postgres).** Sign up at supabase.com with your **personal** email, then *New project*:
   - Name: `solarops-copilot`. Region: **Sydney (ap-southeast-2)**.
   - Generate a strong database password and save it in your password manager.

## 1. Database connection string

In the Supabase project, click **Connect** and copy the **Session pooler** string. Replace `[YOUR-PASSWORD]` with your password and add `?sslmode=require` to the end:

```
postgresql://postgres.<project-ref>:<password>@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require
```

Use the *session* pooler, not *direct*: the direct host is IPv6-only, and Lambda's outbound network is IPv4. Don't use the *transaction* pooler either, because it breaks server-side prepared statements. The tables are created automatically on the first run.

## 2. Bootstrap stack (deploy role and budget)

AWS console → **CloudFormation** → *Create stack* → *With new resources* → **Upload a template file** → `infra/bootstrap.yaml`.

- Stack name: `solarops-bootstrap`
- `BudgetEmail`: your email. The others keep their defaults.
- Tick *I acknowledge that AWS CloudFormation might create IAM resources with custom names*, then **Submit**.

When it reaches `CREATE_COMPLETE`, copy **`DeployRoleArn`** from the *Outputs* tab.

## 3. Secrets (SSM Parameter Store)

AWS console → **Systems Manager** → **Parameter Store** → *Create parameter*. Create three parameters, each of type **SecureString**:

| Name | Value |
|---|---|
| `/solarops/database-url` | The connection string from step 1 |
| `/solarops/openelectricity-api-key` | A key from platform.openelectricity.org.au. Generate a fresh one if an old key was ever shared. |
| `/solarops/api-key` | A long random string, for example from your password manager's generator. Clients send it as `x-api-key`. |

## 4. Connect GitHub

On GitHub, go to the repo → **Settings** → *Secrets and variables* → **Actions** → **Variables** tab → *New repository variable*:

| Variable | Value |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | The ARN from step 2 (required) |
| `ALARM_EMAIL` | Optional. The address for DLQ, backlog and API-error alarms. |
| `LLM_PROVIDER` | Optional. `none` (default) or `bedrock`, see step 6. |

These are *variables*, not secrets, because none of them are sensitive. The actual secrets stay in SSM.

## 5. Deploy

**Actions** → **Deploy** → *Run workflow*. It takes about 5 minutes and runs these steps:

1. `sam build` and `sam deploy` for the `solarops-copilot` stack.
2. Loads the solar farm registry once.
3. Smoke-tests `GET /health` and `POST /v1/ask`.

The run summary shows the API URL. Open `<url>/docs` for the interactive API docs.

- **Ingestion** starts on the next 5-minute tick. Weather fills in within 15 minutes.
- **Performance indexes** only appear in Australian daylight. AEST is Malaysia time + 2 hours.
- **Alarm emails:** if you set `ALARM_EMAIL`, confirm the subscription email that AWS sends.

## 6. Optional: switch the copilot to Amazon Bedrock

1. Open the **Bedrock** console in Sydney and go to *Model access*. Enable **Amazon Nova Lite**.
2. Set the repository variable `LLM_PROVIDER=bedrock` and run **Deploy** again.
3. Answers now come from Nova through the `apac.amazon.nova-lite-v1:0` inference profile, with the router as fallback. `provider` in each response shows which one answered.

To score the model on the golden set from your own machine (you need AWS credentials in your shell):

```bash
PYTHONPATH=src python -m solarops.evals --provider bedrock
```

## Cost

Expect about **US$0/month**.

- **Lambda:** around 20k invocations and about 10k GB-seconds a month, well inside the always-free tier.
- **SQS, EventBridge Scheduler, SSM standard parameters and three CloudWatch alarms:** all free at this scale.
- **API Gateway HTTP API:** free for the first 12 months at this volume, then about a dollar per million requests.
- **S3:** stores about 15 KB per 5-minute file, roughly 150 MB a year, which costs cents.
- **Bedrock** (only if enabled) costs fractions of a cent per question.

The budget from step 2 emails you at 50% of US$5 actual spend, and when the forecast passes 100%.

## Tear down

1. Empty the stack's S3 bucket (`solarops-copilot-rawbucket-…`).
2. Delete the `solarops-copilot` stack.
3. Delete `solarops-bootstrap`.
4. Delete the three SSM parameters and, if you want, the Supabase project.
