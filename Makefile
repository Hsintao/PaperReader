.PHONY: web backend frontend worker

web:
	bash scripts/start_web.sh

backend:
	cd backend && uvicorn app.main:app --reload

frontend:
	cd frontend && npm run dev

worker:
	cd backend && celery -A app.workers.tasks worker -l info
