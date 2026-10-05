const { verifyDelivery: verifyAgentDelivery } = require("../agent-delivery/verify-delivery.cjs");

const POLICY = Object.freeze({
  branchPrefix: "maintainer/",
  ciDescription: "maintenance",
});

function verifyDelivery(args) {
  return verifyAgentDelivery(args, POLICY);
}

module.exports = { verifyDelivery };
