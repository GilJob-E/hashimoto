from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from engine.hashimoto import HashimotoEngine
from models.io import EngineInput

app = FastAPI(title="Hashimoto API", description="API for Hashimoto Interview Engine")

class InitRequest(BaseModel):
    resume_text: str
    topic_count: int = 3

class ProcessTurnRequest(BaseModel):
    stt_text: str
    anxiety: float = 50.0
    confidence: float = 50.0
    turn_number: int = 1

# Global engine instance (for simplicity, in production use sessions)
engine: HashimotoEngine | None = None

@app.post("/init")
def init_engine(request: InitRequest):
    global engine
    try:
        engine = HashimotoEngine.from_resume(request.resume_text, topic_count=request.topic_count)
        topics = [engine.current_topic] + engine.remaining_topics
        return {"topics": topics, "current_topic": engine.current_topic}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/process_turn")
def process_turn(request: ProcessTurnRequest):
    global engine
    if engine is None:
        raise HTTPException(status_code=400, detail="Engine not initialized")
    try:
        input_data = EngineInput(
            stt_text=request.stt_text,
            multimodal={"anxiety": request.anxiety, "confidence": request.confidence},
            turn_number=request.turn_number,
        )
        strategy = engine.process_turn(input_data)
        return strategy.interaction_strategy
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)