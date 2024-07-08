import torch
from torch.utils.data import DataLoader
from transformers import GPT2LMHeadModel, GPT2Tokenizer, AdamW
from datasets import load_dataset

# Load a small dataset
dataset = load_dataset("wikitext", "wikitext-2-raw-v1", split="train[:1000]")

# Initialize tokenizer and model
tokenizer = GPT2Tokenizer.from_pretrained('gpt2')
model = GPT2LMHeadModel.from_pretrained('gpt2')

tokenizer.pad_token = tokenizer.eos_token

# Tokenize the dataset
def tokenize_function(examples):
    return tokenizer(examples["text"], truncation=True, max_length=128, padding="max_length")

tokenized_dataset = dataset.map(tokenize_function, batched=True)
tokenized_dataset = tokenized_dataset.remove_columns(["text"])
tokenized_dataset.set_format("torch")

# Create DataLoader
dataloader = DataLoader(tokenized_dataset, batch_size=4, shuffle=True)

# Training loop
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print('device', device)
model.to(device)

# Evaluation function
def evaluate(model, dataloader):
    model.eval()
    total_loss = 0
    with torch.no_grad():
        for batch in dataloader:
            inputs = batch["input_ids"].to(device)
            outputs = model(input_ids=inputs, labels=inputs)
            total_loss += outputs.loss.item()
    return total_loss / len(dataloader)

# Initial evaluation
initial_loss = evaluate(model, dataloader)
print(f"Initial Loss: {initial_loss:.4f}")
print(f"Initial Perplexity: {torch.exp(torch.tensor(initial_loss)):.4f}")
optimizer = AdamW(model.parameters(), lr=5e-5)

num_epochs = 3
for epoch in range(num_epochs):
    model.train()
    for batch in dataloader:
        batch = {k: v.to(device) for k, v in batch.items()}
        outputs = model(input_ids=batch['input_ids'], labels=batch['input_ids'])
        loss = outputs.loss
        loss.backward()
        optimizer.step()
        optimizer.zero_grad()
    print(f"Epoch {epoch+1}/{num_epochs} completed")

# Final evaluation
final_loss = evaluate(model, dataloader)
print(f"Final Loss: {final_loss:.4f}")
print(f"Final Perplexity: {torch.exp(torch.tensor(final_loss)):.4f}")

print(f"Loss decreased by: {initial_loss - final_loss:.4f}")
print(f"Perplexity decreased by: {torch.exp(torch.tensor(initial_loss)) - torch.exp(torch.tensor(final_loss)):.4f}")