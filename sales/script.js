const form = document.querySelector("#setup-form");
const result = document.querySelector("#request-result");
const messageBox = document.querySelector("#request-message");
const status = document.querySelector("#form-status");
const copyRequest = document.querySelector("#copy-request");
const copyDiscord = document.querySelector("#copy-discord");

function selectedFeatures(data) {
  return data.getAll("features").filter(Boolean);
}

async function copyText(text, successMessage) {
  try {
    await navigator.clipboard.writeText(text);
    status.textContent = successMessage;
  } catch {
    status.textContent = "Copy did not work—select the text and copy it manually.";
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();

  const data = new FormData(form);
  const features = selectedFeatures(data);
  const notes = String(data.get("notes") || "").trim();
  const featureLine = features.length ? features.map((item) => `- ${item}`).join("\n") : "- I’m not sure yet";

  messageBox.value = [
    "Hey, I’m interested in the $35 Discord setup.",
    "",
    `Server: ${data.get("server")}`,
    `Purpose: ${data.get("purpose")}`,
    "Features:",
    featureLine,
    "",
    `Notes: ${notes || "None yet"}`,
  ].join("\n");

  result.hidden = false;
  status.textContent = "Request created. Copy it and send it on Discord.";
  result.scrollIntoView({ behavior: "smooth", block: "nearest" });
});

copyRequest.addEventListener("click", () => copyText(messageBox.value, "Request copied."));
copyDiscord.addEventListener("click", () => copyText("7331.lol", "Discord username copied: 7331.lol"));
