import { App } from "aws-cdk-lib";
import { Match, Template } from "aws-cdk-lib/assertions";
import { CheckpointStack } from "../lib/checkpoint-stack";

const SSM_PREFIX = (env: string) => `/pmat/${env}/alpha-mesh/review-pull-request`;

function synth(deployEnv: string, checkpointTtlDays = 2): Template {
  const app = new App();
  const stack = new CheckpointStack(app, `Test-${deployEnv}`, {
    deployEnv,
    checkpointTtlDays,
  });
  return Template.fromStack(stack);
}

function logicalId(template: Template, type: string): string {
  const ids = Object.keys(template.findResources(type));
  expect(ids).toHaveLength(1);
  return ids[0];
}

describe.each(["dev", "prod"])("CheckpointStack (%s)", (deployEnv) => {
  const template = synth(deployEnv);

  test("table has PK/SK string keys, on-demand billing and TTL attribute 'ttl'", () => {
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: `pmat_${deployEnv}_ALPHA_MESH_REVIEW_CHECKPOINTS`,
      KeySchema: [
        { AttributeName: "PK", KeyType: "HASH" },
        { AttributeName: "SK", KeyType: "RANGE" },
      ],
      AttributeDefinitions: [
        { AttributeName: "PK", AttributeType: "S" },
        { AttributeName: "SK", AttributeType: "S" },
      ],
      BillingMode: "PAY_PER_REQUEST",
      TimeToLiveSpecification: { AttributeName: "ttl", Enabled: true },
    });
  });

  test("bucket blocks public access, uses SSE-S3 and defines no lifecycle rule", () => {
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [
          { ServerSideEncryptionByDefault: { SSEAlgorithm: "AES256" } },
        ],
      },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      // The saver adds its own ttl-days lifecycle rule (spec 3.10)
      LifecycleConfiguration: Match.absent(),
    });
  });

  test("publishes table, bucket and ttl-days to SSM", () => {
    const tableId = logicalId(template, "AWS::DynamoDB::Table");
    const bucketId = logicalId(template, "AWS::S3::Bucket");
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `${SSM_PREFIX(deployEnv)}/checkpoint-table`,
      Value: { Ref: tableId },
    });
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `${SSM_PREFIX(deployEnv)}/checkpoint-bucket`,
      Value: { Ref: bucketId },
    });
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `${SSM_PREFIX(deployEnv)}/checkpoint-ttl-days`,
      Value: "2",
    });
  });

  test("grants no access through IAM", () => {
    template.resourceCountIs("AWS::IAM::ManagedPolicy", 0);
    template.resourceCountIs("AWS::IAM::User", 0);
    template.resourceCountIs("AWS::IAM::Policy", 0);

    const roles = template.findResources("AWS::IAM::Role");
    const bucketPolicies = template.findResources("AWS::S3::BucketPolicy");
    if (deployEnv !== "dev") {
      expect(roles).toEqual({});
      expect(bucketPolicies).toEqual({});
      return;
    }

    // Dev only: the execution role of CDK's Custom::S3AutoDeleteObjects Lambda,
    // which the bucket policy allows to empty the bucket on teardown.
    const autoDelete = Object.keys(template.findResources("Custom::S3AutoDeleteObjects"));
    expect(autoDelete).toHaveLength(1);
    const roleIds = Object.keys(roles);
    expect(roleIds).toHaveLength(1);
    expect(roleIds[0]).toMatch(/^CustomS3AutoDeleteObjectsCustomResourceProviderRole/);
    expect(JSON.stringify(roles)).not.toContain(logicalId(template, "AWS::DynamoDB::Table"));

    const statements = Object.values(bucketPolicies).flatMap(
      (p: any) => p.Properties.PolicyDocument.Statement,
    );
    for (const s of statements.filter((s: any) => s.Effect === "Allow")) {
      expect(s.Principal).toEqual({ AWS: { "Fn::GetAtt": [roleIds[0], "Arn"] } });
    }
  });
});

test("ttl-days comes from checkpointTtlDays", () => {
  synth("dev", 7).hasResourceProperties("AWS::SSM::Parameter", {
    Name: `${SSM_PREFIX("dev")}/checkpoint-ttl-days`,
    Value: "7",
  });
});

test("dev destroys its data on teardown; prod retains it", () => {
  synth("dev").hasResource("AWS::DynamoDB::Table", { DeletionPolicy: "Delete" });
  synth("dev").hasResource("AWS::S3::Bucket", { DeletionPolicy: "Delete" });
  synth("prod").hasResource("AWS::DynamoDB::Table", { DeletionPolicy: "Retain" });
  synth("prod").hasResource("AWS::S3::Bucket", { DeletionPolicy: "Retain" });
});
