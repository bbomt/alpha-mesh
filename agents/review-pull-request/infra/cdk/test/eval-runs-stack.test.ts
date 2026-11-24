import { App } from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import { EvalRunsStack } from "../lib/eval-runs-stack";

function synth(deployEnv: string): Template {
  const app = new App();
  const env = { account: "123456789012", region: "zz-test-1" };
  return Template.fromStack(new EvalRunsStack(app, `Test-${deployEnv}`, { deployEnv, env }));
}

describe.each(["dev", "prod"])("EvalRunsStack (%s)", (deployEnv) => {
  const template = synth(deployEnv);

  test("the bucket is retained on teardown in every env, with no auto-delete", () => {
    template.hasResource("AWS::S3::Bucket", { DeletionPolicy: "Retain", UpdateReplacePolicy: "Retain" });
    template.resourceCountIs("Custom::S3AutoDeleteObjects", 0);
  });

  test("blocks public access, uses SSE-S3 and is versioned", () => {
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: `pmat-${deployEnv}-alpha-mesh-eval-runs-123456789012`,
      BucketEncryption: {
        ServerSideEncryptionConfiguration: [{ ServerSideEncryptionByDefault: { SSEAlgorithm: "AES256" } }],
      },
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      VersioningConfiguration: { Status: "Enabled" },
    });
  });

  test("expires noncurrent versions after 30 days and clears delete markers and stopped uploads", () => {
    template.hasResourceProperties("AWS::S3::Bucket", {
      LifecycleConfiguration: {
        Rules: [
          {
            Status: "Enabled",
            NoncurrentVersionExpiration: { NoncurrentDays: 30 },
            ExpiredObjectDeleteMarker: true,
            AbortIncompleteMultipartUpload: { DaysAfterInitiation: 7 },
          },
        ],
      },
    });
  });

  test("never expires a current version", () => {
    const rules = Object.values(template.findResources("AWS::S3::Bucket")).flatMap(
      (b: any) => b.Properties.LifecycleConfiguration.Rules,
    );
    for (const rule of rules) {
      expect(rule.ExpirationInDays).toBeUndefined();
      expect(rule.ExpirationDate).toBeUndefined();
    }
  });

  test("publishes the bucket to SSM", () => {
    const bucketId = Object.keys(template.findResources("AWS::S3::Bucket"))[0];
    template.hasResourceProperties("AWS::SSM::Parameter", {
      Name: `/pmat/${deployEnv}/alpha-mesh/review-pull-request/eval-runs-bucket`,
      Value: { Ref: bucketId },
    });
  });

  test("grants no access through IAM", () => {
    for (const type of ["AWS::IAM::Role", "AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::User",
                        "AWS::S3::BucketPolicy"]) {
      template.resourceCountIs(type, 0);
    }
  });
});
