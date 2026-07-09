import json

import pulumi
import pulumi_aws as aws
import pulumi_awsx as awsx

# ---------------------------------------------------------------------------
# Config — encrypted secrets and plain values set via
#   pulumi config set --secret <key> <value>   (secrets)
#   pulumi config set <key> <value>             (plain)
# ---------------------------------------------------------------------------
config = pulumi.Config()
fred_api_key = config.require_secret("fredApiKey")
anthropic_api_key = config.require_secret("anthropicApiKey")
anthropic_model = config.require("anthropicModel")
db_password = config.require_secret("dbPassword")

db_username = "modernfi"
db_name = "modernfi_rate_agent"

# ---------------------------------------------------------------------------
# Networking — a VPC with public and private subnets across multiple AZs.
# awsx.ec2.Vpc gives us this in one resource instead of hand-wiring subnets,
# route tables, an internet gateway, and NAT gateways ourselves.
# ---------------------------------------------------------------------------
vpc = awsx.ec2.Vpc(
    "modernfi-vpc",
    nat_gateways=awsx.ec2.NatGatewayConfigurationArgs(
        strategy=awsx.ec2.NatGatewayStrategy.SINGLE,
    ),
)

# ---------------------------------------------------------------------------
# ECS cluster — logical grouping the Fargate service runs inside.
# Plain aws.ecs.Cluster (not awsx — awsx removed its wrapper since it didn't
# add anything over the raw resource).
# ---------------------------------------------------------------------------
cluster = aws.ecs.Cluster("modernfi-cluster")

# ---------------------------------------------------------------------------
# ECR — repository plus build/push of the app's Dockerfile in one step.
# ---------------------------------------------------------------------------
ecr_repository = awsx.ecr.Repository("modernfi-rate-agent-repo")

ecr_image = awsx.ecr.Image(
    "modernfi-rate-agent-image",
    repository_url=ecr_repository.url,
    context="../",
    dockerfile="../Dockerfile",
    platform="linux/amd64",
)

# ---------------------------------------------------------------------------
# Security groups
# ---------------------------------------------------------------------------

# ALB security group — accepts inbound HTTP from the public internet.
alb_security_group = aws.ec2.SecurityGroup(
    "alb-security-group",
    vpc_id=vpc.vpc_id,
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=80,
            to_port=80,
            cidr_blocks=["0.0.0.0/0"],
        )
    ],
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            protocol="-1",
            from_port=0,
            to_port=0,
            cidr_blocks=["0.0.0.0/0"],
        )
    ],
)

# Fargate task security group — only accepts traffic from the ALB, on the
# app's port (8000).
fargate_security_group = aws.ec2.SecurityGroup(
    "fargate-security-group",
    vpc_id=vpc.vpc_id,
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=8000,
            to_port=8000,
            security_groups=[alb_security_group.id],
        )
    ],
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            protocol="-1",
            from_port=0,
            to_port=0,
            cidr_blocks=["0.0.0.0/0"],
        )
    ],
)

# RDS security group — only accepts Postgres traffic (5432) from the Fargate
# task security group, not from the open internet.
rds_security_group = aws.ec2.SecurityGroup(
    "rds-security-group",
    vpc_id=vpc.vpc_id,
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=5432,
            to_port=5432,
            security_groups=[fargate_security_group.id],
        )
    ],
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            protocol="-1",
            from_port=0,
            to_port=0,
            cidr_blocks=["0.0.0.0/0"],
        )
    ],
)

# ---------------------------------------------------------------------------
# RDS Postgres — production database, replacing the local Docker Compose
# Postgres. Lives in the VPC's private subnets (no public access).
# ---------------------------------------------------------------------------
db_subnet_group = aws.rds.SubnetGroup(
    "modernfi-db-subnet-group",
    subnet_ids=vpc.private_subnet_ids,
)

rds_instance = aws.rds.Instance(
    "modernfi-rds",
    engine="postgres",
    engine_version="16",
    instance_class="db.t3.micro",
    allocated_storage=20,
    db_name=db_name,
    username=db_username,
    password=db_password,
    db_subnet_group_name=db_subnet_group.name,
    vpc_security_group_ids=[rds_security_group.id],
    skip_final_snapshot=True,  # fine for a take-home / dev environment
    publicly_accessible=False,
)

# Build the DATABASE_URL from pieces that are only known once the RDS
# instance exists (its endpoint) plus the password secret. pulumi.Output.all
# lets us combine multiple Outputs (including a secret one) into a single
# derived value without ever having the plaintext available outside Pulumi's
# secret-tracking.
database_url = pulumi.Output.all(rds_instance.endpoint, db_password).apply(
    lambda args: f"postgresql://{db_username}:{args[1]}@{args[0]}/{db_name}"
)

# ---------------------------------------------------------------------------
# Secrets Manager — the task definition's `secrets` field (as opposed to
# `environment`) pulls values from here at container start time, so the
# actual secret values never appear in plaintext in the task definition
# JSON (which `environment` would expose in the ECS console/API).
# ---------------------------------------------------------------------------
fred_api_key_secret = aws.secretsmanager.Secret("fred-api-key-secret")
aws.secretsmanager.SecretVersion(
    "fred-api-key-secret-version",
    secret_id=fred_api_key_secret.id,
    secret_string=fred_api_key,
)

anthropic_api_key_secret = aws.secretsmanager.Secret("anthropic-api-key-secret")
aws.secretsmanager.SecretVersion(
    "anthropic-api-key-secret-version",
    secret_id=anthropic_api_key_secret.id,
    secret_string=anthropic_api_key,
)

database_url_secret = aws.secretsmanager.Secret("database-url-secret")
aws.secretsmanager.SecretVersion(
    "database-url-secret-version",
    secret_id=database_url_secret.id,
    secret_string=database_url,
)

# ---------------------------------------------------------------------------
# ECS task execution role — built explicitly with plain aws.iam resources
# rather than relying on awsx's auto-created role, because the default
# AmazonECSTaskExecutionRolePolicy (ECR pull + CloudWatch logs) does NOT
# include secretsmanager:GetSecretValue, and our container pulls three
# secrets at startup via the task definition's `secrets` field. Without this
# extra permission, tasks fail immediately with ResourceInitializationError /
# AccessDeniedException trying to resolve those secrets.
# ---------------------------------------------------------------------------
task_execution_role = aws.iam.Role(
    "modernfi-rate-agent-execution-role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    ),
)

# Standard AWS-managed policy: grants ECR image pull + CloudWatch Logs
# permissions, the baseline every ECS task execution role needs.
aws.iam.RolePolicyAttachment(
    "modernfi-rate-agent-execution-role-managed-policy",
    role=task_execution_role.name,
    policy_arn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
)

# Custom inline policy granting secretsmanager:GetSecretValue, scoped to
# exactly the three secrets this task needs (not "*").
aws.iam.RolePolicy(
    "modernfi-rate-agent-execution-role-secrets-policy",
    role=task_execution_role.id,
    policy=pulumi.Output.all(
        fred_api_key_secret.arn,
        anthropic_api_key_secret.arn,
        database_url_secret.arn,
    ).apply(
        lambda arns: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "secretsmanager:GetSecretValue",
                        "Resource": arns,
                    }
                ],
            }
        )
    ),
)

# ---------------------------------------------------------------------------
# Fargate service with an Application Load Balancer in front of it.
# awsx.ecs.FargateService wires up the task definition and (via
# default_listener/target_group below) the ALB integration in one
# component. The execution role above is passed in explicitly instead of
# letting awsx auto-create one, for the reason described above.
# ---------------------------------------------------------------------------
alb = awsx.lb.ApplicationLoadBalancer(
    "modernfi-alb",
    subnet_ids=vpc.public_subnet_ids,
    security_groups=[alb_security_group.id],
    listener=awsx.lb.ListenerArgs(port=80, protocol="HTTP"),
    default_target_group=awsx.lb.TargetGroupArgs(
        port=8000,
        protocol="HTTP",
        vpc_id=vpc.vpc_id,
        target_type="ip",
        health_check=aws.lb.TargetGroupHealthCheckArgs(
            path="/health",
            healthy_threshold=2,
            unhealthy_threshold=5,
            interval=30,
        ),
    ),
)

fargate_service = awsx.ecs.FargateService(
    "modernfi-rate-agent-service",
    cluster=cluster.arn,
    desired_count=1,
    network_configuration=aws.ecs.ServiceNetworkConfigurationArgs(
        subnets=vpc.private_subnet_ids,
        security_groups=[fargate_security_group.id],
    ),
    task_definition_args=awsx.ecs.FargateServiceTaskDefinitionArgs(
        execution_role=awsx.awsx.DefaultRoleWithPolicyArgs(
            role_arn=task_execution_role.arn,
        ),
        container=awsx.ecs.TaskDefinitionContainerDefinitionArgs(
            name="modernfi-rate-agent",
            image=ecr_image.image_uri,
            cpu=256,
            memory=512,
            essential=True,
            port_mappings=[
                awsx.ecs.TaskDefinitionPortMappingArgs(
                    container_port=8000,
                    target_group=alb.default_target_group,
                )
            ],
            environment=[
                awsx.ecs.TaskDefinitionKeyValuePairArgs(
                    name="ANTHROPIC_MODEL",
                    value=anthropic_model,
                ),
            ],
            secrets=[
                awsx.ecs.TaskDefinitionSecretArgs(
                    name="FRED_API_KEY",
                    value_from=fred_api_key_secret.arn,
                ),
                awsx.ecs.TaskDefinitionSecretArgs(
                    name="ANTHROPIC_API_KEY",
                    value_from=anthropic_api_key_secret.arn,
                ),
                awsx.ecs.TaskDefinitionSecretArgs(
                    name="DATABASE_URL",
                    value_from=database_url_secret.arn,
                ),
            ],
        ),
    ),
)

# ---------------------------------------------------------------------------
# GitHub Actions OIDC — lets CI authenticate to AWS without storing any
# long-lived access keys as GitHub secrets. GitHub mints a short-lived,
# per-workflow-run identity token; AWS trusts it via this OIDC provider and
# hands out temporary credentials scoped to whichever role the token's
# claims are allowed to assume.
#
# Two roles, deliberately split by blast radius:
#   - preview role: assumable from ANY branch/PR in this repo, read-only
#     (`ReadOnlyAccess`) — safe to run on untrusted PRs, since it can plan
#     but never change anything.
#   - deploy role: assumable ONLY from a workflow run targeting the
#     `production` GitHub environment, full access (`AdministratorAccess`
#     — reasonable for this personal/sandbox account; a shared/production
#     account should scope this down to the specific services this stack
#     actually manages). Scoped on `environment:production`, not
#     `ref:refs/heads/main`, because a job that declares `environment:` has
#     its OIDC token's `sub` claim replaced with the environment name
#     instead of the branch ref — matching on the ref here would silently
#     never match once the job has an environment attached, exactly the
#     bug this comment used to describe before it was caught.
# ---------------------------------------------------------------------------
github_repo = "jwoodruff/modernfi-rate-agent"

github_oidc_provider = aws.iam.OpenIdConnectProvider(
    "github-actions-oidc",
    url="https://token.actions.githubusercontent.com",
    client_id_lists=["sts.amazonaws.com"],
    # GitHub's well-known OIDC token-signing certificate thumbprint. AWS
    # validates GitHub's provider against its own trusted CA bundle
    # regardless of what's set here, but the field is still required at
    # creation time.
    thumbprint_lists=["6938fd4d98bab03faadb97b34396831e3780aea1"],
)


def _github_oidc_trust_policy(oidc_provider_arn: str, subject_pattern: str) -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Federated": oidc_provider_arn},
                    "Action": "sts:AssumeRoleWithWebIdentity",
                    "Condition": {
                        "StringEquals": {
                            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
                        },
                        "StringLike": {
                            "token.actions.githubusercontent.com:sub": subject_pattern
                        },
                    },
                }
            ],
        }
    )


github_actions_preview_role = aws.iam.Role(
    "github-actions-preview-role",
    assume_role_policy=github_oidc_provider.arn.apply(
        lambda arn: _github_oidc_trust_policy(arn, f"repo:{github_repo}:*")
    ),
)

aws.iam.RolePolicyAttachment(
    "github-actions-preview-role-readonly",
    role=github_actions_preview_role.name,
    policy_arn="arn:aws:iam::aws:policy/ReadOnlyAccess",
)

github_actions_deploy_role = aws.iam.Role(
    "github-actions-deploy-role",
    assume_role_policy=github_oidc_provider.arn.apply(
        lambda arn: _github_oidc_trust_policy(
            arn, f"repo:{github_repo}:environment:production"
        )
    ),
)

aws.iam.RolePolicyAttachment(
    "github-actions-deploy-role-admin",
    role=github_actions_deploy_role.name,
    policy_arn="arn:aws:iam::aws:policy/AdministratorAccess",
)

# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------
pulumi.export("ecr_repository_url", ecr_repository.url)
pulumi.export("ecr_image_uri", ecr_image.image_uri)
pulumi.export("alb_url", alb.load_balancer.dns_name.apply(lambda dns: f"http://{dns}"))
pulumi.export("rds_endpoint", rds_instance.endpoint)
pulumi.export("github_actions_preview_role_arn", github_actions_preview_role.arn)
pulumi.export("github_actions_deploy_role_arn", github_actions_deploy_role.arn)
