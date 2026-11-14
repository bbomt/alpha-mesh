import { Duration, RemovalPolicy, Stack, StackProps } from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as ssm from "aws-cdk-lib/aws-ssm";
import { Construct } from "constructs";

export interface EvalRunsStackProps extends StackProps {
  deployEnv: string;
}

// The eval runs that committed files depend on (PMAT-336; eval/scripts/archive_runs.py).
// Its own stack, so a checkpoint-stack teardown (DESTROY with auto-delete in dev) can't touch it.
// Retained in every env. Versioned: a deleted or replaced run can be restored for 30 days, then goes.
// No IAM resources: the agent runs on EC2 through the SSO dev-EC2 permission set.
export class EvalRunsStack extends Stack {
  constructor(scope: Construct, id: string, props: EvalRunsStackProps) {
    super(scope, id, props);

    const { deployEnv } = props;

    const bucket = new s3.Bucket(this, "EvalRunsBucket", {
      bucketName: `pmat-${deployEnv}-alpha-mesh-eval-runs-${this.account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: true,
      removalPolicy: RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          noncurrentVersionExpiration: Duration.days(30),
          expiredObjectDeleteMarker: true,
          abortIncompleteMultipartUploadAfter: Duration.days(7),
        },
      ],
    });

    new ssm.StringParameter(this, "EvalRunsBucketParam", {
      parameterName: `/pmat/${deployEnv}/alpha-mesh/review-pull-request/eval-runs-bucket`,
      stringValue: bucket.bucketName,
    });
  }
}
