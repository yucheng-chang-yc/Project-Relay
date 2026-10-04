model <- readRDS("wb_inputs/prior-model.rds")
result <- read.csv("wb_inputs/prior-result.csv")
stopifnot(nrow(result) == 6, inherits(model, "lm"))
write.csv(data.frame(temperature = 25, prediction = as.numeric(predict(model, data.frame(temperature = 25)))),
          "followup.csv", row.names = FALSE)
