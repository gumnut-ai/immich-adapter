const { verifyDelivery: verifyAgentDelivery } = require("../agent-delivery/verify-delivery.cjs");

const POLICY = Object.freeze({
  branchPrefix: "librarian/",
  ciDescription: "documentation",
});

function verifyDelivery(args) {
  return verifyAgentDelivery(args, POLICY);
}

module.exports = { verifyDelivery };
