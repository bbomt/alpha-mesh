import { App } from "aws-cdk-lib";
import { CheckpointStack } from "../lib/checkpoint-stack";

const DEPLOY_ENVS = ["dev", "staging", "prod"];

const app = new App();

// Passed to cdk deploy as -c config=dev|staging|prod
const deployEnv: string = app.node.tryGetContext("config");
if (!DEPLOY_ENVS.includes(deployEnv)) {
  throw new Error(
    `Environment config missing or invalid: "${deployEnv}". Use: cdk deploy -c config=dev|staging|prod`
  );
}

const { checkpointTtlDays } = app.node.tryGetContext(deployEnv) ?? {};
if (!Number.isInteger(checkpointTtlDays) || checkpointTtlDays < 1) {
  throw new Error(`cdk.json context "${deployEnv}.checkpointTtlDays" must be a positive integer`);
}

new CheckpointStack(app, "PmatAlphaMeshReviewCheckpointsStack", {
  stackName: `PmatAlphaMeshReviewCheckpointsStack-${deployEnv}`,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
  deployEnv,
  checkpointTtlDays,
});
